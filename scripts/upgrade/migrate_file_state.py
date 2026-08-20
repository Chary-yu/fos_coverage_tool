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

from app.progress.file_state_service import (
    query_project_progress_aggregated,
    query_authoritative_progress
)

logger = logging.getLogger(__name__)

def backfill_file_state_for_project(connection, project_name: str) -> Dict[str, Any]:
    """Backfill coverage_file_state for a single project from historical facts."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT data_version FROM coverage_project_state WHERE project_name = %s", (project_name,))
        p_row = cursor.fetchone()
        current_version = int((p_row.get("data_version") if isinstance(p_row, dict) else p_row[0])) if p_row else 1
        
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
                %s as data_version
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
        """
        cursor.execute(sql_insert_batch, (current_version, project_name))
        connection.commit()
        
        return {
            "project_name": project_name,
            "data_version": current_version,
            "status": "BACKFILLED"
        }

def backfill_all_projects(connection) -> Dict[str, Any]:
    """Idempotently backfill every project and require exact reconciliation."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT DISTINCT project_name FROM coverage_line_index ORDER BY project_name")
        rows = cursor.fetchall() or []
    projects = [r[0] if not isinstance(r, dict) else r.get("project_name") for r in rows]
    reports = []
    for project in projects:
        backfill_file_state_for_project(connection, project)
        ok, report = reconcile_project_progress(connection, project)
        reports.append(report)
        if not ok:
            raise RuntimeError("coverage_file_state reconciliation failed for {}".format(project))
        # Only an exact reconciliation may advance the project-level derived
        # readiness marker.  Authoritative facts remain untouched.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE coverage_project_state SET file_state_version = data_version, updated_at = NOW(6) WHERE project_name = %s",
                (project,),
            )
        connection.commit()
    return {"status": "READY", "projects": reports}

def reconcile_project_progress(connection, project_name: str) -> Tuple[bool, Dict[str, Any]]:
    """
    Reconcile aggregated coverage_file_state summary against true authoritative facts.
    Returns (is_exact_match, diff_report).
    """
    # 1. Query derived aggregate table
    agg = query_project_progress_aggregated(
        connection,
        project_name,
        fallback_authoritative=False,
        require_project_readiness=False,
    )
    # 2. Query true authoritative facts table directly
    auth = query_authoritative_progress(connection, project_name)
    
    diff = {}
    fields = [
        "total_uncovered", "filled_total", "draft_total", 
        "confirmed_total", "coverable_total", "uncoverable_total", 
        "redundant_total", "file_count"
    ]
    for f in fields:
        agg_val = agg.get(f, 0)
        auth_val = auth.get(f, 0)
        if agg_val != auth_val:
            diff[f] = {"aggregated": agg_val, "authoritative": auth_val}
            
    is_match = (len(diff) == 0 and agg.get("source") == "coverage_file_state")
    return is_match, {
        "project_name": project_name,
        "reconciled": is_match,
        "diff": diff,
        "aggregated_summary": agg,
        "authoritative_summary": auth
    }

if __name__ == "__main__":
    print("File State Migration & Backfill Module ready.")
