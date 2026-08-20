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
    return list(row.values()) if isinstance(row, dict) else list(row)

def get_project_aggregate_readiness(connection, project_name: str) -> Dict[str, Any]:
    """Return explicit readiness instead of silently labelling a fallback a hit."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT data_version, file_state_version FROM coverage_project_state WHERE project_name = %s",
            (project_name,)
        )
        row = cursor.fetchone()
        if isinstance(row, dict):
            expected = int(row.get("data_version") or 1)
            ready_version = int(row.get("file_state_version") or 0)
        else:
            expected = int(row[0]) if row else 1
            ready_version = int(row[1]) if row and len(row) > 1 else 0
        cursor.execute(
            "SELECT COUNT(*), MIN(data_version), MAX(data_version) FROM coverage_file_state WHERE project_name = %s",
            (project_name,),
        )
        aggregate = cursor.fetchone() or (0, 0, 0)
    aggregate = _row_values(aggregate)
    ready = (ready_version == expected and int(aggregate[0] or 0) > 0 and
             int(aggregate[1] or 0) == expected and int(aggregate[2] or 0) == expected)
    return {
        "project_name": project_name,
        "expected_data_version": expected,
        "file_state_version": ready_version,
        "file_count": int(aggregate[0] or 0),
        "min_data_version": int(aggregate[1] or 0),
        "max_data_version": int(aggregate[2] or 0),
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
        try:
            cursor.execute(
                "SELECT data_version, file_state_version FROM coverage_project_state WHERE project_name = %s",
                (project_name,)
            )
            p_row = cursor.fetchone()
            if p_row:
                if isinstance(p_row, dict):
                    expected_version = int(p_row.get("data_version") or 1)
                    file_state_version = int(p_row.get("file_state_version") or 0)
                else:
                    expected_version = int(p_row[0])
                    # Compatibility with pre-readiness test doubles only;
                    # real MySQL rows always contain both selected columns.
                    file_state_version = int(p_row[1]) if len(p_row) > 1 else expected_version
        except Exception:
            expected_version = 1
            file_state_version = 0

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
            values = _row_values(row) if row else []
            if values and values[0] > 0:
                file_count = int(values[0])
                min_v = int(values[8])
                max_v = int(values[9])
                
                # Check version freshness
                aggregate_version_ok = min_v == expected_version and max_v == expected_version
                project_ready = file_state_version == expected_version
                if aggregate_version_ok and (project_ready or not require_project_readiness):
                    return {
                        "source": "coverage_file_state",
                        "aggregate_ready": project_ready,
                        "data_version": expected_version,
                        "file_state_version": file_state_version,
                        "file_count": file_count,
                        "total_uncovered": int(values[1]),
                        "filled_total": int(values[2]),
                        "draft_total": int(values[3]),
                        "confirmed_total": int(values[4]),
                        "coverable_total": int(values[5]),
                        "uncoverable_total": int(values[6]),
                        "redundant_total": int(values[7]),
                        "pending_unconfirmed": max(0, int(values[1]) - int(values[4]))
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
        if row:
            values = _row_values(row)
            total_uncovered = int(values[1] or 0)
            confirmed_total = int(values[4] or 0)
            return {
                "source": "authoritative_facts",
                "file_count": int(values[0] or 0),
                "total_uncovered": total_uncovered,
                "filled_total": int(values[2] or 0),
                "draft_total": int(values[3] or 0),
                "confirmed_total": confirmed_total,
                "coverable_total": int(values[5] or 0),
                "uncoverable_total": int(values[6] or 0),
                "redundant_total": int(values[7] or 0),
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

        values = _row_values(row)
        total_uncovered = int(values[0] or 0)
        filled_total = int(values[1] or 0)
        draft_total = int(values[2] or 0)
        confirmed_total = int(values[3] or 0)
        coverable_total = int(values[4] or 0)
        uncoverable_total = int(values[5] or 0)
        redundant_total = int(values[6] or 0)

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
        if isinstance(state, dict):
            version = int(state.get("data_version") or 1)
        else:
            version = int(state[0] or 1)

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
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE coverage_project_state SET file_state_version = %s, updated_at = NOW(6) WHERE project_name = %s",
            (version if ready else 0, project_name),
        )
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
