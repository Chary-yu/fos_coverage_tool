"""
Coverage File State Aggregation Layer (Item 7)
Derived progress aggregation table management:
- Maintains project-level aggregate statistics from coverage_file_state
- Dual updates on review save and line-index sync
- Validates data_version against coverage_project_state to prevent stale aggregate reads
- Automatic fallback to authoritative query if file state table is missing, empty, or stale
"""

import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

def _row_values(row):
    """Normalize driver rows without treating arbitrary test doubles as rows."""
    if row is None:
        return []
    if isinstance(row, dict):
        return list(row.values())
    if isinstance(row, (list, tuple)):
        return list(row)
    try:
        return list(row)
    except (TypeError, ValueError):
        return []


def _row_field(row, names, position, default=None):
    """Read one selected column without depending on DictCursor ordering."""
    if isinstance(row, dict):
        for name in names:
            if name in row:
                return row.get(name)
        return default
    values = _row_values(row)
    if len(values) > position:
        return values[position]
    return default


def _int_value(value, default=None):
    """Convert a database scalar while keeping malformed rows fail-closed."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _ordered_row_values(row, names):
    """Return selected fields in SQL order for tuple and dict cursors."""
    if isinstance(row, dict):
        if not all(name in row for name in names):
            return []
        return [row.get(name) for name in names]
    return _row_values(row)

def get_project_aggregate_readiness(connection, project_name: str) -> Dict[str, Any]:
    """Return explicit readiness instead of silently labelling a fallback a hit."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT data_version, file_state_version FROM coverage_project_state WHERE project_name = %s",
            (project_name,)
        )
        row = cursor.fetchone()
        expected_raw = _row_field(row, ("data_version",), 0)
        ready_raw = _row_field(row, ("file_state_version",), 1)
        expected = _int_value(expected_raw, 1)
        ready_version = _int_value(ready_raw, 0)
        state_valid = bool(row is not None and expected_raw is not None and
                           _int_value(expected_raw) is not None)
        cursor.execute(
            "SELECT COUNT(*) AS file_count, "
            "MIN(data_version) AS min_version, "
            "MAX(data_version) AS max_version "
            "FROM coverage_file_state WHERE project_name = %s",
            (project_name,),
        )
        aggregate = cursor.fetchone() or (0, 0, 0)
    aggregate = _ordered_row_values(
        aggregate, ("file_count", "min_version", "max_version")
    )
    if len(aggregate) < 3:
        aggregate = [0, 0, 0]
    file_count = _int_value(aggregate[0], 0)
    min_version = _int_value(aggregate[1], 0)
    max_version = _int_value(aggregate[2], 0)
    ready = (state_valid and ready_version == expected and file_count > 0 and
             min_version == expected and max_version == expected)
    return {
        "project_name": project_name,
        "expected_data_version": expected,
        "file_state_version": ready_version,
        "file_count": file_count,
        "min_data_version": min_version,
        "max_data_version": max_version,
        "ready": ready,
    }

def query_project_progress_aggregated(
    connection,
    project_name: str,
    fallback_authoritative: bool = True,
    require_project_readiness: bool = True,
) -> Dict[str, Any]:
    """
    Query project progress using derived coverage_file_state.
    Validates that coverage_file_state.data_version matches coverage_project_state.data_version.
    If stale or unpopulated and fallback_authoritative is True, falls back to authoritative query.
    """
    with connection.cursor() as cursor:
        # 1. Fetch expected project data_version
        expected_version = 1
        file_state_version = 0
        project_state_valid = False
        try:
            cursor.execute(
                "SELECT data_version, file_state_version FROM coverage_project_state WHERE project_name = %s",
                (project_name,)
            )
            p_row = cursor.fetchone()
            expected_raw = _row_field(p_row, ("data_version",), 0)
            expected_candidate = _int_value(expected_raw)
            if expected_candidate is not None:
                expected_version = expected_candidate
                ready_raw = _row_field(p_row, ("file_state_version",), 1)
                # Compatibility with pre-readiness tuple test doubles only;
                # real MySQL rows always contain both selected columns.
                if ready_raw is None and not isinstance(p_row, dict):
                    p_values = _row_values(p_row)
                    file_state_version = (
                        expected_version if len(p_values) == 1 else 0
                    )
                else:
                    file_state_version = _int_value(ready_raw, 0)
                project_state_valid = True
        except Exception:
            expected_version = 1
            file_state_version = 0
            project_state_valid = False

        # 2. Try aggregated coverage_file_state query with version validation
        try:
            sql = """
                SELECT COUNT(*) as file_count,
                       COALESCE(SUM(total_uncovered), 0) as total_uncovered,
                       COALESCE(SUM(filled_total), 0) as filled_total,
                       COALESCE(SUM(draft_total), 0) as draft_total,
                       COALESCE(SUM(confirmed_total), 0) as confirmed_total,
                       COALESCE(SUM(coverable_total), 0) as coverable_total,
                       COALESCE(SUM(uncoverable_total), 0) as uncoverable_total,
                       COALESCE(SUM(redundant_total), 0) as redundant_total,
                       COALESCE(MIN(data_version), 0) as min_version,
                       COALESCE(MAX(data_version), 0) as max_version
                FROM coverage_file_state
                WHERE project_name = %s
            """
            cursor.execute(sql, (project_name,))
            row = cursor.fetchone()
            values = _ordered_row_values(row, (
                "file_count", "total_uncovered", "filled_total", "draft_total",
                "confirmed_total", "coverable_total", "uncoverable_total",
                "redundant_total", "min_version", "max_version",
            )) if row else []
            if len(values) >= 10:
                file_count = _int_value(values[0])
                min_v = _int_value(values[8])
                max_v = _int_value(values[9])
                metrics = [_int_value(value) for value in values[:8]]
            else:
                file_count = min_v = max_v = None
                metrics = []
            if (file_count is not None and file_count > 0 and
                    min_v is not None and max_v is not None and
                    all(value is not None for value in metrics)):
                
                # Check version freshness
                aggregate_version_ok = min_v == expected_version and max_v == expected_version
                project_ready = (project_state_valid and
                                 file_state_version == expected_version)
                if aggregate_version_ok and project_state_valid and (
                        project_ready or not require_project_readiness):
                    return {
                        "source": "coverage_file_state",
                        "aggregate_ready": project_ready,
                        "data_version": expected_version,
                        "file_state_version": file_state_version,
                        "file_count": file_count,
                        "total_uncovered": metrics[1],
                        "filled_total": metrics[2],
                        "draft_total": metrics[3],
                        "confirmed_total": metrics[4],
                        "coverable_total": metrics[5],
                        "uncoverable_total": metrics[6],
                        "redundant_total": metrics[7],
                        "pending_unconfirmed": max(0, metrics[1] - metrics[4])
                    }
                else:
                    logger.info(f"[FileStateService] Stale aggregate detected for '{project_name}' (min={min_v}, max={max_v}, expected={expected_version}, file_state_version={file_state_version}). Falling back.")
        except Exception as e:
            logger.warning(f"[FileStateService] Failed to query coverage_file_state: {e}")

        if not fallback_authoritative:
            return {"source": "empty_or_stale", "total_uncovered": 0, "confirmed_total": 0}

        # 3. Direct Authoritative Query (Bypasses coverage_file_state completely)
        fallback = query_authoritative_progress(connection, project_name)
        fallback["aggregate_ready"] = False
        fallback["data_version"] = expected_version
        fallback["file_state_version"] = file_state_version
        fallback["fallback_reason"] = "aggregate_not_ready"
        return fallback

def query_authoritative_progress(connection, project_name: str) -> Dict[str, Any]:
    """Execute direct authoritative count over coverage_line_index and coverage_analysis."""
    with connection.cursor() as cursor:
        sql_fallback = """
            SELECT COUNT(DISTINCT idx.file_path_hash) as file_count,
                   COUNT(idx.line_number) as total_uncovered,
                   COUNT(ana.line_number) as filled_total,
                   SUM(CASE WHEN ana.is_draft = 1 THEN 1 ELSE 0 END) as draft_total,
                   SUM(CASE WHEN ana.is_draft = 0 AND ana.status IN ('可覆盖', '无法覆盖', '冗余代码') THEN 1 ELSE 0 END) as confirmed_total,
                   SUM(CASE WHEN ana.is_draft = 0 AND ana.status = '可覆盖' THEN 1 ELSE 0 END) as coverable_total,
                   SUM(CASE WHEN ana.is_draft = 0 AND ana.status = '无法覆盖' THEN 1 ELSE 0 END) as uncoverable_total,
                   SUM(CASE WHEN ana.is_draft = 0 AND ana.status = '冗余代码' THEN 1 ELSE 0 END) as redundant_total
            FROM coverage_line_index idx
            LEFT JOIN coverage_analysis ana 
                   ON idx.project_name = ana.project_name 
                  AND idx.file_path_hash = ana.file_path_hash 
                  AND idx.line_number = ana.line_number
            WHERE idx.project_name = %s
        """
        cursor.execute(sql_fallback, (project_name,))
        row = cursor.fetchone()
        values = _ordered_row_values(row, (
            "file_count", "total_uncovered", "filled_total", "draft_total",
            "confirmed_total", "coverable_total", "uncoverable_total",
            "redundant_total",
        ))
        if len(values) >= 8:
            numeric = [_int_value(value, 0) for value in values[:8]]
            total_uncovered = numeric[1]
            confirmed_total = numeric[4]
            return {
                "source": "authoritative_facts",
                "file_count": numeric[0],
                "total_uncovered": total_uncovered,
                "filled_total": numeric[2],
                "draft_total": numeric[3],
                "confirmed_total": confirmed_total,
                "coverable_total": numeric[5],
                "uncoverable_total": numeric[6],
                "redundant_total": numeric[7],
                "pending_unconfirmed": max(0, total_uncovered - confirmed_total)
            }
        return {"source": "empty", "total_uncovered": 0, "confirmed_total": 0}

def update_file_state_for_file(
    connection,
    project_name: str,
    file_path: str,
    file_path_hash: str,
    data_version: int = 1
) -> None:
    """Recalculate and upsert single file state into coverage_file_state."""
    with connection.cursor() as cursor:
        sql_calc = """
            SELECT COUNT(idx.line_number) as total_uncovered,
                   COUNT(ana.line_number) as filled_total,
                   SUM(CASE WHEN ana.is_draft = 1 THEN 1 ELSE 0 END) as draft_total,
                   SUM(CASE WHEN ana.is_draft = 0 AND ana.status IN ('可覆盖', '无法覆盖', '冗余代码') THEN 1 ELSE 0 END) as confirmed_total,
                   SUM(CASE WHEN ana.is_draft = 0 AND ana.status = '可覆盖' THEN 1 ELSE 0 END) as coverable_total,
                   SUM(CASE WHEN ana.is_draft = 0 AND ana.status = '无法覆盖' THEN 1 ELSE 0 END) as uncoverable_total,
                   SUM(CASE WHEN ana.is_draft = 0 AND ana.status = '冗余代码' THEN 1 ELSE 0 END) as redundant_total
            FROM coverage_line_index idx
            LEFT JOIN coverage_analysis ana 
                   ON idx.project_name = ana.project_name 
                  AND idx.file_path_hash = ana.file_path_hash 
                  AND idx.line_number = ana.line_number
            WHERE idx.project_name = %s AND idx.file_path_hash = %s
        """
        cursor.execute(sql_calc, (project_name, file_path_hash))
        row = cursor.fetchone()
        if not row:
            return

        values = _ordered_row_values(row, (
            "total_uncovered", "filled_total", "draft_total", "confirmed_total",
            "coverable_total", "uncoverable_total", "redundant_total",
        ))
        if len(values) < 7:
            return
        numeric = [_int_value(value, 0) for value in values[:7]]
        total_uncovered = numeric[0]
        filled_total = numeric[1]
        draft_total = numeric[2]
        confirmed_total = numeric[3]
        coverable_total = numeric[4]
        uncoverable_total = numeric[5]
        redundant_total = numeric[6]

        sql_upsert = """
            INSERT INTO coverage_file_state (
                project_name, file_path_hash, file_path, total_uncovered,
                filled_total, draft_total, confirmed_total, coverable_total,
                uncoverable_total, redundant_total, data_version
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                file_path = VALUES(file_path),
                total_uncovered = VALUES(total_uncovered),
                filled_total = VALUES(filled_total),
                draft_total = VALUES(draft_total),
                confirmed_total = VALUES(confirmed_total),
                coverable_total = VALUES(coverable_total),
                uncoverable_total = VALUES(uncoverable_total),
                redundant_total = VALUES(redundant_total),
                data_version = VALUES(data_version)
        """
        cursor.execute(sql_upsert, (
            project_name, file_path_hash, file_path, total_uncovered,
            filled_total, draft_total, confirmed_total, coverable_total,
            uncoverable_total, redundant_total, data_version
        ))


def mark_project_aggregate_stale(connection, project_name: str) -> None:
    """Invalidate the derived readiness marker without touching source facts."""
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE coverage_project_state SET file_state_version = 0, updated_at = NOW(6) WHERE project_name = %s",
            (project_name,),
        )


def mark_project_aggregate_ready(connection, project_name: str,
                                 data_version: int) -> bool:
    """Publish Legacy derived readiness only for the observed version.

    Legacy remains an explicitly transitional runtime, but its migration and
    background rebuild paths must obey the same stale-before-ready rule as
    VNext.  Keeping this compare-and-set in one owner prevents a caller from
    promoting a projection after a concurrent authoritative version change.
    """
    expected_version = _int_value(data_version)
    if expected_version is None:
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT data_version FROM coverage_project_state "
            "WHERE project_name = %s",
            (project_name,),
        )
        state = cursor.fetchone()
        if not state:
            return False
        observed_version = _int_value(
            _row_field(state, ("data_version",), 0)
        )
        if observed_version is None:
            return False
        if observed_version != expected_version:
            return False
        cursor.execute(
            "UPDATE coverage_project_state SET file_state_version = %s, "
            "updated_at = NOW(6) WHERE project_name = %s "
            "AND data_version = %s",
            (expected_version, project_name, expected_version),
        )
        # A concurrent authoritative version advance can make the conditional
        # UPDATE affect zero rows.  Re-read the pair instead of trusting the
        # UPDATE result so legacy callers cannot publish a false-ready marker.
        cursor.execute(
            "SELECT data_version, file_state_version "
            "FROM coverage_project_state WHERE project_name = %s",
            (project_name,),
        )
        published = cursor.fetchone()
    if not published:
        return False
    published_data_version = _int_value(
        _row_field(published, ("data_version",), 0)
    )
    published_file_state_version = _int_value(
        _row_field(published, ("file_state_version",), 1)
    )
    if published_data_version is None or published_file_state_version is None:
        return False
    return (published_data_version == expected_version and
            published_file_state_version == expected_version)


def rebuild_project_file_state(connection, project_name: str, commit: bool = False) -> Dict[str, Any]:
    """Rebuild one project's derived rows and advance readiness only on exact match.

    The operation is additive and never updates ``coverage_analysis`` or
    ``coverage_line_index``.  Callers that are already inside a larger write
    transaction pass ``commit=False``; the caller owns the final commit.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT data_version FROM coverage_project_state WHERE project_name = %s",
            (project_name,),
        )
        state = cursor.fetchone()
        if not state:
            return {"project_name": project_name, "status": "NO_PROJECT_STATE", "ready": False}
        version = _int_value(_row_field(state, ("data_version",), 0))
        if version is None:
            return {"project_name": project_name, "status": "INVALID_PROJECT_STATE", "ready": False}

        cursor.execute(
            """
            INSERT INTO coverage_file_state (
                project_name, file_path_hash, file_path, total_uncovered,
                filled_total, draft_total, confirmed_total, coverable_total,
                uncoverable_total, redundant_total, data_version
            )
            SELECT idx.project_name, idx.file_path_hash, COALESCE(MAX(idx.file_path), ''),
                   COUNT(idx.line_number),
                   COUNT(ana.line_number),
                   SUM(CASE WHEN ana.is_draft = 1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN ana.is_draft = 0 AND ana.status IN ('可覆盖', '无法覆盖', '冗余代码') THEN 1 ELSE 0 END),
                   SUM(CASE WHEN ana.is_draft = 0 AND ana.status = '可覆盖' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN ana.is_draft = 0 AND ana.status = '无法覆盖' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN ana.is_draft = 0 AND ana.status = '冗余代码' THEN 1 ELSE 0 END),
                   %s
            FROM coverage_line_index idx
            LEFT JOIN coverage_analysis ana
              ON idx.project_name = ana.project_name
             AND idx.file_path_hash = ana.file_path_hash
             AND idx.line_number = ana.line_number
            WHERE idx.project_name = %s
            GROUP BY idx.project_name, idx.file_path_hash
            ON DUPLICATE KEY UPDATE
                file_path = VALUES(file_path),
                total_uncovered = VALUES(total_uncovered),
                filled_total = VALUES(filled_total),
                draft_total = VALUES(draft_total),
                confirmed_total = VALUES(confirmed_total),
                coverable_total = VALUES(coverable_total),
                uncoverable_total = VALUES(uncoverable_total),
                redundant_total = VALUES(redundant_total),
                data_version = VALUES(data_version)
            """,
            (version, project_name),
        )
        # Remove derived rows for files that no longer exist in the index.  This
        # is safe because coverage_file_state is not authoritative.
        cursor.execute(
            """
            DELETE s FROM coverage_file_state s
            LEFT JOIN coverage_line_index idx
              ON idx.project_name COLLATE utf8mb4_unicode_ci = s.project_name COLLATE utf8mb4_unicode_ci
             AND idx.file_path_hash COLLATE utf8mb4_unicode_ci = s.file_path_hash COLLATE utf8mb4_unicode_ci
            WHERE s.project_name = %s AND idx.file_path_hash IS NULL
            """,
            (project_name,),
        )

    derived = query_project_progress_aggregated(
        connection, project_name, fallback_authoritative=False,
        require_project_readiness=False,
    )
    authoritative = query_authoritative_progress(connection, project_name)
    fields = (
        "total_uncovered", "filled_total", "draft_total", "confirmed_total",
        "coverable_total", "uncoverable_total", "redundant_total", "file_count",
    )
    differences = {
        field: {"derived": derived.get(field, 0), "authoritative": authoritative.get(field, 0)}
        for field in fields if derived.get(field, 0) != authoritative.get(field, 0)
    }
    ready = not differences and derived.get("source") == "coverage_file_state"
    if ready:
        ready_published = mark_project_aggregate_ready(
            connection, project_name, version
        )
    else:
        mark_project_aggregate_stale(connection, project_name)
        ready_published = False
    ready = bool(ready and ready_published)
    if commit and hasattr(connection, "commit"):
        connection.commit()
    return {
        "project_name": project_name,
        "data_version": version,
        "status": "READY" if ready else "FALLBACK_REQUIRED",
        "ready": ready,
        "differences": differences,
        "derived": derived,
        "authoritative": authoritative,
    }
