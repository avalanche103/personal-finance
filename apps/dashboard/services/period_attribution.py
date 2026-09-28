from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable, Iterable

from django.utils import timezone

from apps.accounts.models import Transaction
from apps.accounts.services.balance import transaction_affects_account_balance
from apps.common.services.bynex_trades import BYNEX_CASH_ASSETS, is_cash_like_product


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
    nets_against_deposits: bool = False


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

        fee_events: set[str] = set()
        for leg in self.legs:
            is_in_scope = (
                leg.entity_type == 'account'
                and leg.entity_id in account_scope
                or leg.entity_type == 'product'
                and leg.entity_id in product_scope
            )
            if is_in_scope:
                event_totals[leg.event_key] += leg.signed_usd
                if leg.nets_against_deposits:
                    fee_events.add(leg.event_key)

        contributions = ZERO
        withdrawals = ZERO
        fee_reductions = ZERO
        for event_key, value in event_totals.items():
            if event_key in fee_events and value < 0:
                fee_reductions += -value
            elif value > 0:
                contributions += value
            elif value < 0:
                withdrawals += -value
        if fee_reductions:
            contributions -= min(contributions, fee_reductions)
        return FlowTotals(contributions_usd=contributions, withdrawals_usd=withdrawals)


def _metadata(transaction: Transaction) -> dict:
    return transaction.metadata if isinstance(transaction.metadata, dict) else {}


def _event_key(transaction: Transaction) -> str:
    metadata = _metadata(transaction)
    transfer_pair_id = metadata.get('transfer_pair_id')
    if transfer_pair_id:
        return f'transfer:{transfer_pair_id}'
    return f'transaction:{transaction.id}'


INTERNAL_WALLET_CONVERSION_KINDS = frozenset({
    'spot_buy_credit',
})


def _is_capitalized_income(transaction: Transaction) -> bool:
    if transaction.transaction_type != Transaction.TransactionType.INCOME:
        return False
    metadata = _metadata(transaction)
    return (
        metadata.get('operation_kind') == 'capitalization'
        or metadata.get('interest_mode') == 'capitalized'
    )


def _is_cash_like_asset(value: str) -> bool:
    return (value or '').strip().upper() in BYNEX_CASH_ASSETS


def is_deposit_reducing_fee(transaction: Transaction) -> bool:
    """Network/transfer fees reduce Deposit totals instead of counting as Withdrawal."""
    if transaction.transaction_type != Transaction.TransactionType.FEE:
        return False
    metadata = _metadata(transaction)
    return metadata.get('source') == 'bynex'


def _is_internal_wallet_conversion(transaction: Transaction) -> bool:
    metadata = _metadata(transaction)
    if metadata.get('operation_kind') in INTERNAL_WALLET_CONVERSION_KINDS:
        return True
    if metadata.get('operation_kind') == 'incoming_from_bynex':
        return True
    if (
        transaction.transaction_type == Transaction.TransactionType.TRADE
        and (
            _is_cash_like_asset(str(metadata.get('base_asset') or ''))
            or _is_cash_like_product_leg(transaction)
        )
    ):
        return True
    if (
        transaction.transaction_type == Transaction.TransactionType.TRANSFER
        and metadata.get('source') == 'bynex'
        and str(metadata.get('destination') or '').strip().casefold() == 'binance'
    ):
        return True
    return False


def _is_cash_like_product_leg(transaction: Transaction) -> bool:
    metadata = _metadata(transaction)
    if _is_cash_like_asset(str(metadata.get('asset') or metadata.get('base_asset') or '')):
        return True
    return is_cash_like_product(getattr(transaction, 'product', None))


def _account_signed_flow(transaction: Transaction, magnitude_usd: Decimal) -> Decimal:
    if not magnitude_usd or _is_capitalized_income(transaction) or _is_internal_wallet_conversion(transaction):
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
    if (
        not transaction.product_id
        or not magnitude_usd
        or _is_capitalized_income(transaction)
        or _is_cash_like_product_leg(transaction)
    ):
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
        nets_against_deposits = is_deposit_reducing_fee(transaction)

        if transaction.account_id in account_scope:
            signed_account_flow = _account_signed_flow(transaction, magnitude_usd)
            if signed_account_flow:
                legs.append(
                    FlowLeg(
                        event_key=event_key,
                        entity_type='account',
                        entity_id=transaction.account_id,
                        signed_usd=signed_account_flow,
                        nets_against_deposits=nets_against_deposits,
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
                        nets_against_deposits=nets_against_deposits,
                    )
                )

    return PeriodFlowLedger(legs=legs)
