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
    mark_project_aggregate_stale,
    rebuild_project_file_state,
    query_project_progress_aggregated,
    query_authoritative_progress
)

logger = logging.getLogger(__name__)

def backfill_file_state_for_project(connection, project_name: str) -> Dict[str, Any]:
    """Backfill one legacy project through the shared projection owner.

    The legacy schema is intentionally different from the VNext
    ``scan_id/file_id`` schema, so this adapter remains available for the
    previous-release runtime.  It must not carry a second aggregation or Ready
    implementation, however: ``rebuild_project_file_state`` owns stale,
    reconciliation and compare-and-set publication for this schema.
    """
    # Make stale visible before the rebuild transaction can expose partially
    # refreshed derived rows to another Legacy reader.
    mark_project_aggregate_stale(connection, project_name)
    if hasattr(connection, "commit"):
        connection.commit()
    report = rebuild_project_file_state(connection, project_name, commit=False)
    if hasattr(connection, "commit"):
        connection.commit()
    return report

def backfill_all_projects(connection) -> Dict[str, Any]:
    """Idempotently backfill every project and require exact reconciliation."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT DISTINCT project_name FROM coverage_line_index ORDER BY project_name")
        rows = cursor.fetchall() or []
    projects = [r[0] if not isinstance(r, dict) else r.get("project_name") for r in rows]
    reports = []
    for project in projects:
        rebuild_report = backfill_file_state_for_project(connection, project)
        if not rebuild_report.get("ready"):
            raise RuntimeError(
                "coverage_file_state rebuild did not reach Ready for {}: {}".format(
                    project, rebuild_report.get("differences", {})
                )
            )
        ok, reconciliation = reconcile_project_progress(connection, project)
        if not ok:
            raise RuntimeError("coverage_file_state reconciliation failed for {}".format(project))
        # ``rebuild_project_file_state`` already performed the sole
        # schema-specific compare-and-set Ready write.  Keep the second
        # reconciliation as evidence, but never publish readiness from this
        # orchestrator again.
        reconciliation["ready_owner"] = "app.progress.file_state_service.rebuild_project_file_state"
        reconciliation["rebuild"] = rebuild_report
        reports.append(reconciliation)
        if hasattr(connection, "commit"):
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
