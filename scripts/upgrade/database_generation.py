"""Fail-closed database generation detection for upgrade orchestration.

The release lane has two different historical data models.  Keeping their
classification in a tiny dependency-free module prevents an upgrade caller
from accidentally sending an existing VNext database through the Legacy
snapshot/migration code path.
"""

from __future__ import print_function

from app.db.repositories.base import fetchall, is_sqlite


LEGACY = "LEGACY"
VNEXT = "VNEXT"
UNKNOWN = "UNKNOWN"

# Public aliases make the names unambiguous at call sites and keep the values
# stable for evidence consumers.
DATABASE_GENERATION_LEGACY = LEGACY
DATABASE_GENERATION_VNEXT = VNEXT
DATABASE_GENERATION_UNKNOWN = UNKNOWN

LEGACY_REQUIRED_TABLES = frozenset((
    "coverage_line_index",
    "coverage_analysis",
))

VNEXT_REQUIRED_TABLES = frozenset((
    "coverage_projects",
    "coverage_scans",
    "coverage_lines",
    "coverage_project_state",
))


def database_table_names(connection):
    """Return a normalized, sorted table inventory for one connection."""
    if is_sqlite(connection):
        rows = fetchall(
            connection,
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name",
        )
        names = []
        for row in rows:
            value = row.get("name") if isinstance(row, dict) else None
            if value:
                names.append(str(value))
        return sorted(set(names))

    rows = fetchall(connection, """
        SELECT TABLE_NAME
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME
    """)
    names = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("TABLE_NAME") or row.get("table_name")
        if value:
            names.append(str(value))
    return sorted(set(names))


def inspect_database_generation(connection):
    """Describe whether *connection* is Legacy, VNext, or unknown.

    A hybrid database containing both complete signatures is deliberately
    classified as ``UNKNOWN``.  It is safer to stop than to guess which model
    owns overlapping tables.
    """
    try:
        tables = database_table_names(connection)
    except Exception as exc:
        return {
            "status": "FAILED",
            "generation": UNKNOWN,
            "tables": [],
            "legacy_signature": [],
            "vnext_signature": [],
            "reason": "TABLE_PROBE_FAILED",
            "error": str(exc),
        }

    table_set = set(name.lower() for name in tables)
    legacy_signature = sorted(
        table for table in LEGACY_REQUIRED_TABLES if table.lower() in table_set
    )
    vnext_signature = sorted(
        table for table in VNEXT_REQUIRED_TABLES if table.lower() in table_set
    )
    has_legacy = len(legacy_signature) == len(LEGACY_REQUIRED_TABLES)
    has_vnext = len(vnext_signature) == len(VNEXT_REQUIRED_TABLES)

    if has_legacy and has_vnext:
        generation = UNKNOWN
        reason = "AMBIGUOUS_LEGACY_AND_VNEXT_SIGNATURES"
    elif has_legacy:
        generation = LEGACY
        reason = "LEGACY_SIGNATURE"
    elif has_vnext:
        generation = VNEXT
        reason = "VNEXT_SIGNATURE"
    else:
        generation = UNKNOWN
        reason = "NO_COMPLETE_SCHEMA_SIGNATURE"

    return {
        "status": "PASSED",
        "generation": generation,
        "tables": tables,
        "legacy_signature": legacy_signature,
        "vnext_signature": vnext_signature,
        "reason": reason,
    }


def classify_database(connection):
    """Return only the stable ``LEGACY``/``VNEXT``/``UNKNOWN`` value."""
    return inspect_database_generation(connection).get("generation", UNKNOWN)


def require_database_generation(connection, expected):
    """Require one exact generation and return its inspection evidence."""
    expected = str(expected or "").strip().upper()
    if expected not in (LEGACY, VNEXT):
        raise ValueError("expected database generation must be LEGACY or VNEXT")
    result = inspect_database_generation(connection)
    if result.get("generation") != expected:
        raise RuntimeError(
            "database generation mismatch: expected {}, observed {} ({})".format(
                expected, result.get("generation"), result.get("reason", "")
            )
        )
    return result

