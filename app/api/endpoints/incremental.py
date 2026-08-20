def request(body):
    body = body or {}
    project_name = str(body.get("project_name") or "").strip()
    required = ("scan_id", "repo_path", "oldgit", "newgit", "info_path")
    if not project_name or any(not body.get(key) for key in required):
        raise ValueError("project_name, scan_id, repo_path, oldgit, newgit and info_path are required")
    return project_name
