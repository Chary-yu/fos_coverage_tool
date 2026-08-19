"""
Coverage File State Aggregation Layer (Item 7)
Derived progress aggregation table management:
- Maintains project-level aggregate statistics from coverage_file_state
- Dual updates on review save and line-index sync
- Fast O(Files) progress aggregation vs slow O(Lines) scans
- Automatic fallback to authoritative query if file state table is unpopulated
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
    If coverage_file_state has no records for project and fallback_authoritative is True,
    falls back to legacy authoritative query over coverage_analysis and coverage_line_index.
    """
    with connection.cursor() as cursor:
        # 1. Try aggregated coverage_file_state query
        try:
            sql = """
                SELECT COUNT(*) as file_count,
                       COALESCE(SUM(total_uncovered), 0) as total_uncovered,
                       COALESCE(SUM(filled_total), 0) as filled_total,
                       COALESCE(SUM(draft_total), 0) as draft_total,
                       COALESCE(SUM(confirmed_total), 0) as confirmed_total,
                       COALESCE(SUM(coverable_total), 0) as coverable_total,
                       COALESCE(SUM(uncoverable_total), 0) as uncoverable_total,
                       COALESCE(SUM(redundant_total), 0) as redundant_total
                FROM coverage_file_state
                WHERE project_name = %s
            """
            cursor.execute(sql, (project_name,))
            row = cursor.fetchone()
            if row and row[0] > 0: # file_count > 0
                return {
                    "source": "coverage_file_state",
                    "file_count": int(row[0]),
                    "total_uncovered": int(row[1]),
                    "filled_total": int(row[2]),
                    "draft_total": int(row[3]),
                    "confirmed_total": int(row[4]),
                    "coverable_total": int(row[5]),
                    "uncoverable_total": int(row[6]),
                    "redundant_total": int(row[7]),
                    "pending_unconfirmed": max(0, int(row[1]) - int(row[4]))
                }
        except Exception as e:
            logger.warning(f"[FileStateService] Failed to query coverage_file_state, checking fallback: {e}")

        if not fallback_authoritative:
            return {"source": "empty", "total_uncovered": 0, "confirmed_total": 0}

        # 2. Legacy Authoritative Fallback Query
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
                "source": "authoritative_fallback",
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
    """
    Recalculate and upsert single file state into coverage_file_state.
    Safe and idempotent.
    """
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
