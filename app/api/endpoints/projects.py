def project_name(body):
    value = str((body or {}).get("project_name") or "").strip()
    if not value:
        raise ValueError("project_name is required")
    return value


def scan_id(value):
    if value in (None, ""):
        raise ValueError("scan_id is required")
    return int(value)
