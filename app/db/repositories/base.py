"""DB-API helpers shared by repositories.

The production driver uses percent-s placeholders while SQLite uses question
mark placeholders. Keeping this adaptation in one module prevents SQL dialect
details leaking into services and makes migration tests deterministic.
"""

from typing import Any, Dict, Iterable, Optional


def is_sqlite(connection) -> bool:
    connection = getattr(connection, "_connection", connection)
    module = getattr(connection.__class__, "__module__", "")
    return module.startswith("sqlite3") or (
        connection.__class__.__name__ in ("Connection", "Cursor")
        and "sqlite" in module
    )


def adapt_sql(connection, sql: str) -> str:
    if is_sqlite(connection):
        return sql
    return sql.replace("?", "%s")


def execute(connection, sql: str, params: Iterable[Any] = ()):
    cursor = connection.cursor()
    cursor.execute(adapt_sql(connection, sql), tuple(params or ()))
    return cursor


def _description(cursor):
    return [item[0] for item in (getattr(cursor, "description", None) or [])]


def row_to_dict(cursor, row) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return dict(zip(_description(cursor), row))


def fetchone(connection, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
    cursor = execute(connection, sql, params)
    try:
        return row_to_dict(cursor, cursor.fetchone())
    finally:
        cursor.close()


def fetchall(connection, sql: str, params: Iterable[Any] = ()):
    cursor = execute(connection, sql, params)
    try:
        return [row_to_dict(cursor, row) for row in cursor.fetchall()]
    finally:
        cursor.close()


def insert_id(cursor, fallback: int = 0) -> int:
    value = getattr(cursor, "lastrowid", None)
    try:
        return int(value or fallback)
    except (TypeError, ValueError):
        return int(fallback)
