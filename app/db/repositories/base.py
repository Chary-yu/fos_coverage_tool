"""DB-API helpers shared by repositories.

The production driver uses percent-s placeholders while SQLite uses question
mark placeholders. Keeping this adaptation in one module prevents SQL dialect
details leaking into services and makes migration tests deterministic.
"""

import time
from typing import Any, Dict, Iterable, Optional

from app.observability.performance import current_collector


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


def db_capabilities(connection) -> Dict[str, Any]:
    """Return conservative bind/query capabilities for the active driver.

    SQLite's portable parameter limit is 999. Some newer builds raise it,
    but keeping the repository contract below that limit makes the same code
    safe on older embedded and MariaDB-compatible test environments. MySQL
    drivers do not need the SQLite restriction, while bounded chunks still
    protect packet size and query planning.
    """
    return {
        "dialect": "sqlite" if is_sqlite(connection) else "mysql",
        "max_bind_params": 900 if is_sqlite(connection) else 2000,
        "preferred_chunk_size": 450 if is_sqlite(connection) else 500,
    }


def bind_chunk_size(connection, parameter_width=1, reserved=0, maximum=None):
    capabilities = db_capabilities(connection)
    available = max(1, int(capabilities["max_bind_params"]) - int(reserved or 0))
    size = max(1, available // max(1, int(parameter_width)))
    if maximum is not None:
        size = min(size, max(1, int(maximum)))
    return size


def execute(connection, sql: str, params: Iterable[Any] = ()):
    cursor = connection.cursor()
    started = time.perf_counter()
    try:
        cursor.execute(adapt_sql(connection, sql), tuple(params or ()))
    finally:
        collector = _collector_for(connection)
        if collector is not None:
            collector.record_db_query((time.perf_counter() - started) * 1000.0)
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
        row = cursor.fetchone()
        collector = _collector_for(connection)
        if collector is not None:
            collector.record_db_rows(1 if row is not None else 0)
        return row_to_dict(cursor, row)
    finally:
        cursor.close()


def fetchall(connection, sql: str, params: Iterable[Any] = ()):
    cursor = execute(connection, sql, params)
    try:
        rows = cursor.fetchall()
        collector = _collector_for(connection)
        if collector is not None:
            collector.record_db_rows(len(rows))
        return [row_to_dict(cursor, row) for row in rows]
    finally:
        cursor.close()


def iter_rows(connection, sql: str, params: Iterable[Any] = (), batch_size=500):
    """Yield DB rows in bounded batches without materializing a result set."""
    cursor = execute(connection, sql, params)
    columns = _description(cursor)
    size = max(1, int(batch_size or 500))
    try:
        while True:
            rows = cursor.fetchmany(size)
            if not rows:
                break
            collector = _collector_for(connection)
            if collector is not None:
                collector.record_db_rows(len(rows))
            for row in rows:
                yield row_to_dict(cursor, row) if not isinstance(row, dict) else dict(row)
    finally:
        cursor.close()


def insert_id(cursor, fallback: int = 0) -> int:
    value = getattr(cursor, "lastrowid", None)
    try:
        return int(value or fallback)
    except (TypeError, ValueError):
        return int(fallback)


def _collector_for(connection):
    return current_collector() or getattr(connection, "_performance_collector", None)
