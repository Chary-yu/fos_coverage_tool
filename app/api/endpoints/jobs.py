def request(body):
    body = body or {}
    kind = str(body.get("kind") or "").strip()
    if not kind or body.get("scan_id") in (None, ""):
        raise ValueError("kind and scan_id are required")
    return kind, int(body["scan_id"])
