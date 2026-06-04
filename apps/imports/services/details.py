FINSTORE_RECORD_FIELDS = (
    'operation_type',
    'token_name',
    'quantity',
    'amount',
    'amount_currency',
    'occurred_at',
)


def build_job_details(result) -> dict:
    editable_records = result.artifacts.get('normalized_records') or result.records
    payload = result.to_dict()
    payload['editable_records'] = editable_records
    payload['record_fields'] = infer_record_fields(editable_records)
    return payload


def get_editable_records(job) -> list[dict]:
    details = job.details or {}
    if details.get('editable_records'):
        return details['editable_records']
    return details.get('records', [])


def infer_record_fields(records: list[dict]) -> list[str]:
    if not records:
        return []
    if all(field in records[0] for field in FINSTORE_RECORD_FIELDS):
        return list(FINSTORE_RECORD_FIELDS)
    keys: list[str] = []
    for record in records:
        for key in record:
            if key not in keys and key != 'row_number':
                keys.append(key)
    return keys


def update_editable_record(job, row_index: int, field: str, value: str) -> tuple[bool, str]:
    records = get_editable_records(job)
    if row_index < 0 or row_index >= len(records):
        return False, 'Row not found.'
    if field not in records[row_index]:
        return False, 'Field not found.'
    if field == 'row_number':
        return False, 'Row number is read-only.'

    records[row_index][field] = value
    details = dict(job.details or {})
    details['editable_records'] = records
    details['records'] = records[:25]
    job.details = details
    job.save(update_fields=['details', 'updated_at'])
    return True, ''
