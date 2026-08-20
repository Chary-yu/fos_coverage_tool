def project_name(query, default=""):
    value = str((query or {}).get("project") or (query or {}).get("project_name") or default).strip()
    if not value:
        raise ValueError("project is required")
    return value
