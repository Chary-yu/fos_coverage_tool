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

def query_project_progress_aggregated(
    connection,
    project_name: str,
    fallback_authoritative: bool = True
) -> Dict[str, Any]:
    """
    Query project progress using derived coverage_file_state.
    Validates that coverage_file_state.data_version matches coverage_project_state.data_version.
    If stale or unpopulated and fallback_authoritative is True, falls back to authoritative query.
    """
    with connection.cursor() as cursor:
        # 1. Fetch expected project data_version
        expected_version = 1
        try:
            cursor.execute("SELECT data_version FROM coverage_project_state WHERE project_name = %s", (project_name,))
            p_row = cursor.fetchone()
            if p_row:
                expected_version = int(p_row[0])
        except Exception:
            expected_version = 1

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
            if row and row[0] > 0:
                file_count = int(row[0])
                min_v = int(row[8])
                max_v = int(row[9])
                
                # Check version freshness
                if min_v == expected_version and max_v == expected_version:
                    return {
                        "source": "coverage_file_state",
                        "data_version": expected_version,
                        "file_count": file_count,
                        "total_uncovered": int(row[1]),
                        "filled_total": int(row[2]),
                        "draft_total": int(row[3]),
                        "confirmed_total": int(row[4]),
                        "coverable_total": int(row[5]),
                        "uncoverable_total": int(row[6]),
                        "redundant_total": int(row[7]),
                        "pending_unconfirmed": max(0, int(row[1]) - int(row[4]))
                    }
                else:
                    logger.info(f"[FileStateService] Stale aggregate detected for '{project_name}' (min={min_v}, max={max_v}, expected={expected_version}). Falling back.")
        except Exception as e:
            logger.warning(f"[FileStateService] Failed to query coverage_file_state: {e}")

        if not fallback_authoritative:
            return {"source": "empty_or_stale", "total_uncovered": 0, "confirmed_total": 0}

        # 3. Direct Authoritative Query (Bypasses coverage_file_state completely)
        return query_authoritative_progress(connection, project_name)

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
            total_uncovered = int(row[1] or 0)
            confirmed_total = int(row[4] or 0)
            return {
                "source": "authoritative_facts",
                "file_count": int(row[0] or 0),
                "total_uncovered": total_uncovered,
                "filled_total": int(row[2] or 0),
                "draft_total": int(row[3] or 0),
                "confirmed_total": confirmed_total,
                "coverable_total": int(row[5] or 0),
                "uncoverable_total": int(row[6] or 0),
                "redundant_total": int(row[7] or 0),
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

        total_uncovered = int(row[0] or 0)
        filled_total = int(row[1] or 0)
        draft_total = int(row[2] or 0)
        confirmed_total = int(row[3] or 0)
        coverable_total = int(row[4] or 0)
        uncoverable_total = int(row[5] or 0)
        redundant_total = int(row[6] or 0)

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
