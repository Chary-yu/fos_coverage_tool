"""
Additive Migration & Idempotent Backfill Script (Item 8)
Populates the derived coverage_file_state table from historical coverage_line_index and coverage_analysis.
Performs automatic reconciliation against authoritative query to prove zero data discrepancy.
"""

import os
import sys
import time
import json
import logging
from typing import Dict, Any, List, Tuple, Optional

logger = logging.getLogger(__name__)

def backfill_file_state_for_project(connection, project_name: str) -> Dict[str, Any]:
    """
    Backfill coverage_file_state for a single project from historical facts.
    """
    with connection.cursor() as cursor:
        sql_distinct_files = """
            SELECT DISTINCT file_path_hash, COALESCE(file_path, '')
            FROM coverage_line_index
            WHERE project_name = %s
        """
        cursor.execute(sql_distinct_files, (project_name,))
        files = cursor.fetchall()
        
        sql_insert_batch = """
            INSERT INTO coverage_file_state (
                project_name, file_path_hash, file_path, total_uncovered,
                filled_total, draft_total, confirmed_total, coverable_total,
                uncoverable_total, redundant_total, data_version
            )
            SELECT 
                idx.project_name,
                idx.file_path_hash,
                COALESCE(MAX(idx.file_path), ''),
                COUNT(idx.line_number) as total_uncovered,
                COUNT(ana.line_number) as filled_total,
                SUM(CASE WHEN ana.is_draft = 1 THEN 1 ELSE 0 END) as draft_total,
                SUM(CASE WHEN ana.is_draft = 0 AND ana.status IN ('可覆盖', '无法覆盖', '冗余代码') THEN 1 ELSE 0 END) as confirmed_total,
                SUM(CASE WHEN ana.is_draft = 0 AND ana.status = '可覆盖' THEN 1 ELSE 0 END) as coverable_total,
                SUM(CASE WHEN ana.is_draft = 0 AND ana.status = '无法覆盖' THEN 1 ELSE 0 END) as uncoverable_total,
                SUM(CASE WHEN ana.is_draft = 0 AND ana.status = '冗余代码' THEN 1 ELSE 0 END) as redundant_total,
                1 as data_version
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
                redundant_total = VALUES(redundant_total)
        """
        cursor.execute(sql_insert_batch, (project_name,))
        connection.commit()
        
        return {
            "project_name": project_name,
            "files_processed": len(files),
            "status": "BACKFILLED"
        }

def reconcile_project_progress(connection, project_name: str) -> Tuple[bool, Dict[str, Any]]:
    """
    Reconcile aggregated coverage_file_state summary against authoritative query.
    Returns (is_exact_match, diff_report).
    """
    from app.progress.file_state_service import query_project_progress_aggregated
    
    with connection.cursor() as cursor:
        agg = query_project_progress_aggregated(connection, project_name, fallback_authoritative=False)
        auth = query_project_progress_aggregated(connection, project_name, fallback_authoritative=True)
        
        diff = {}
        fields = ["total_uncovered", "filled_total", "draft_total", "confirmed_total", "coverable_total", "uncoverable_total", "redundant_total"]
        for f in fields:
            agg_val = agg.get(f, 0)
            auth_val = auth.get(f, 0)
            if agg_val != auth_val:
                diff[f] = {"aggregated": agg_val, "authoritative": auth_val}
                
        is_match = (len(diff) == 0)
        return is_match, {
            "project_name": project_name,
            "reconciled": is_match,
            "diff": diff,
            "aggregated_summary": agg,
            "authoritative_summary": auth
        }

if __name__ == "__main__":
    print("File State Migration & Backfill Module ready.")
