"""UTC time helpers shared by runtime, persistence, and evidence code.

Database timestamps in this repository intentionally remain naive UTC strings
for compatibility with the existing schema. New code should use these helpers
instead of calling the deprecated ``datetime.utcnow`` directly.
"""

from datetime import datetime, timezone


def utc_now():
    """Return an aware UTC datetime."""
    return datetime.now(timezone.utc)


def utc_now_naive():
    """Return naive UTC for legacy SQL DATETIME columns."""
    return utc_now().replace(tzinfo=None)


def utc_sql():
    """Return the repository's SQL timestamp representation."""
    return utc_now_naive().strftime("%Y-%m-%d %H:%M:%S")


def utc_iso():
    """Return an RFC3339-like UTC timestamp used in evidence payloads."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
