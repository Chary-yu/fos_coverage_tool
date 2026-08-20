def identity(source):
    source = source or {}
    scan_id = source.get("scan_id")
    report_id = str(source.get("report_id") or "")
    repository_name = str(source.get("repository_name") or "")
    file_path = str(source.get("file_path") or "")
    if not scan_id or not report_id or not file_path:
        raise ValueError("scan_id, report_id and file_path are required")
    return int(scan_id), report_id, repository_name, file_path
