FINSTORE_REDEMPTION_OPERATIONS = {
    'Возврат инвестиций',
    'Досрочное погашение токенов',
}


def is_finstore_redemption_operation(operation_type: str) -> bool:
    normalized_operation = (operation_type or '').strip()
    return normalized_operation in FINSTORE_REDEMPTION_OPERATIONS
