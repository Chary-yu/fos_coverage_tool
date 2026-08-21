"""Stable, bounded identity fingerprints for MariaDB 5.5 indexes."""

import hashlib
import json


def stable_identity_hash(*values):
    """Hash a typed, ordered identity tuple with one canonical encoding."""
    payload = json.dumps(
        list(values), ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
