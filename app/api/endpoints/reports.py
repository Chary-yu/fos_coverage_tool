def report_id(value):
    value = str(value or "").strip()
    if not value:
        raise ValueError("report_id is required")
    return value
