from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable, Iterable

from django.utils import timezone

from apps.accounts.models import Transaction
from apps.accounts.services.balance import transaction_affects_account_balance


ZERO = Decimal('0')


@dataclass(frozen=True)
class FlowTotals:
    contributions_usd: Decimal = ZERO
    withdrawals_usd: Decimal = ZERO


@dataclass(frozen=True)
class FlowLeg:
    event_key: str
    entity_type: str
    entity_id: int
    signed_usd: Decimal


@dataclass
class PeriodFlowLedger:
    legs: list[FlowLeg]

    def totals(
        self,
        *,
        account_ids: Iterable[int] = (),
        product_ids: Iterable[int] = (),
    ) -> FlowTotals:
        account_scope = set(account_ids)
        product_scope = set(product_ids)
        event_totals: dict[str, Decimal] = defaultdict(lambda: ZERO)

        for leg in self.legs:
            is_in_scope = (
                leg.entity_type == 'account'
                and leg.entity_id in account_scope
                or leg.entity_type == 'product'
                and leg.entity_id in product_scope
            )
            if is_in_scope:
                event_totals[leg.event_key] += leg.signed_usd

        contributions = sum((value for value in event_totals.values() if value > 0), ZERO)
        withdrawals = sum((-value for value in event_totals.values() if value < 0), ZERO)
        return FlowTotals(contributions_usd=contributions, withdrawals_usd=withdrawals)


def _metadata(transaction: Transaction) -> dict:
    return transaction.metadata if isinstance(transaction.metadata, dict) else {}


def _event_key(transaction: Transaction) -> str:
    metadata = _metadata(transaction)
    transfer_pair_id = metadata.get('transfer_pair_id')
    if transfer_pair_id:
        return f'transfer:{transfer_pair_id}'
    return f'transaction:{transaction.id}'


def _is_capitalized_income(transaction: Transaction) -> bool:
    if transaction.transaction_type != Transaction.TransactionType.INCOME:
        return False
    metadata = _metadata(transaction)
    return (
        metadata.get('operation_kind') == 'capitalization'
        or metadata.get('interest_mode') == 'capitalized'
    )


def _account_signed_flow(transaction: Transaction, magnitude_usd: Decimal) -> Decimal:
    if not magnitude_usd or _is_capitalized_income(transaction):
        return ZERO

    tx_type = transaction.transaction_type
    product_linked = bool(transaction.product_id)
    has_economic_account_leg = transaction_affects_account_balance(transaction) or (
        product_linked
        and tx_type in (
            Transaction.TransactionType.TRADE,
            Transaction.TransactionType.FEE,
        )
    )
    if not has_economic_account_leg:
        return ZERO

    if product_linked:
        if tx_type == Transaction.TransactionType.TRADE:
            quantity = transaction.quantity or ZERO
            return -magnitude_usd if quantity >= 0 else magnitude_usd
        if tx_type == Transaction.TransactionType.FEE:
            return -magnitude_usd
        if tx_type == Transaction.TransactionType.INCOME:
            return magnitude_usd
        amount = transaction.amount or ZERO
        if amount:
            return magnitude_usd if amount > 0 else -magnitude_usd
        return ZERO

    amount = transaction.amount or ZERO
    if tx_type in (
        Transaction.TransactionType.DEPOSIT,
        Transaction.TransactionType.WITHDRAWAL,
        Transaction.TransactionType.TRANSFER,
    ):
        if amount > 0:
            return magnitude_usd
        if amount < 0:
            return -magnitude_usd
    return ZERO


def _product_signed_flow(transaction: Transaction, magnitude_usd: Decimal) -> Decimal:
    if not transaction.product_id or not magnitude_usd or _is_capitalized_income(transaction):
        return ZERO

    tx_type = transaction.transaction_type
    quantity = transaction.quantity or ZERO
    if tx_type == Transaction.TransactionType.DEPOSIT:
        return magnitude_usd
    if tx_type == Transaction.TransactionType.TRADE:
        return magnitude_usd if quantity >= 0 else -magnitude_usd
    if tx_type == Transaction.TransactionType.INCOME:
        return -magnitude_usd
    if tx_type in (
        Transaction.TransactionType.WITHDRAWAL,
        Transaction.TransactionType.TRANSFER,
    ):
        return -magnitude_usd
    if tx_type == Transaction.TransactionType.FEE:
        return magnitude_usd
    return ZERO


def build_period_flow_ledger(
    transactions: Iterable[Transaction],
    *,
    reference_date: date,
    as_of_date: date,
    amount_usd_resolver: Callable[[Transaction], Decimal],
    account_ids: Iterable[int],
    product_ids: Iterable[int],
) -> PeriodFlowLedger:
    account_scope = set(account_ids)
    product_scope = set(product_ids)
    legs: list[FlowLeg] = []
    seen_transaction_ids: set[int] = set()

    for transaction in transactions:
        if transaction.id in seen_transaction_ids:
            continue
        seen_transaction_ids.add(transaction.id)
        transaction_date = timezone.localtime(transaction.occurred_at).date()
        if transaction_date <= reference_date or transaction_date > as_of_date:
            continue

        magnitude_usd = abs(amount_usd_resolver(transaction))
        if not magnitude_usd:
            continue
        event_key = _event_key(transaction)

        if transaction.account_id in account_scope:
            signed_account_flow = _account_signed_flow(transaction, magnitude_usd)
            if signed_account_flow:
                legs.append(
                    FlowLeg(
                        event_key=event_key,
                        entity_type='account',
                        entity_id=transaction.account_id,
                        signed_usd=signed_account_flow,
                    )
                )

        if transaction.product_id in product_scope:
            signed_product_flow = _product_signed_flow(transaction, magnitude_usd)
            if signed_product_flow:
                legs.append(
                    FlowLeg(
                        event_key=event_key,
                        entity_type='product',
                        entity_id=transaction.product_id,
                        signed_usd=signed_product_flow,
                    )
                )

    return PeriodFlowLedger(legs=legs)
