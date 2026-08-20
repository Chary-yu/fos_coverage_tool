def request(body):
    body = body or {}
    project_name = str(body.get("project_name") or "").strip()
    if not project_name or body.get("scan_id") in (None, ""):
        raise ValueError("project_name and scan_id are required")
    records = body.get("records") or []
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    return project_name, int(body["scan_id"]), records
