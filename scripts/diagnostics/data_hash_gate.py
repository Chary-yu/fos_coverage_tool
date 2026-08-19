"""
Data Hash Gate Module (Item 19)
Calculates deterministic content-level SHA256 hashes and row counts across core MySQL tables:
- coverage_analysis
- coverage_line_index
- coverage_project_state
- coverage_background_jobs
Provides snapshot capture and pre/post migration diffing to ensure ZERO data loss.
"""

import os
import sys
import json
import hashlib
from typing import Dict, Any, Optional, Tuple, List

try:
    from datetime import datetime, timezone
    def get_utc_iso():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(timezone, "utc") else datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
except ImportError:
    from datetime import datetime
    def get_utc_iso():
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def hash_analysis_records(cursor, chunk_size: int = 10000) -> Tuple[int, str]:
    """Compute deterministic SHA256 over business fields of coverage_analysis."""
    sql = """
        SELECT project_name, file_path_hash, line_number, 
               COALESCE(reviewer, ''), COALESCE(status, ''), COALESCE(is_draft, 0),
               COALESCE(coverage_method, ''), COALESCE(uncovered_reason, '')
        FROM coverage_analysis
        ORDER BY project_name, file_path_hash, line_number
    """
    cursor.execute(sql)
    hasher = hashlib.sha256()
    count = 0
    while True:
        rows = cursor.fetchmany(chunk_size)
        if not rows:
            break
        for r in rows:
            count += 1
            line = "|".join(str(val) for val in r) + "\n"
            hasher.update(line.encode("utf-8"))
    return count, hasher.hexdigest()

def hash_line_index_records(cursor, chunk_size: int = 10000) -> Tuple[int, str]:
    """Compute deterministic SHA256 over business fields of coverage_line_index."""
    sql = """
        SELECT project_name, file_path_hash, line_number,
               COALESCE(block_start_line, 0), COALESCE(block_end_line, 0),
               COALESCE(block_type, ''), COALESCE(function_hash, ''),
               COALESCE(code_line_hash, ''), COALESCE(code_occurrence, 0)
        FROM coverage_line_index
        ORDER BY project_name, file_path_hash, line_number
    """
    cursor.execute(sql)
    hasher = hashlib.sha256()
    count = 0
    while True:
        rows = cursor.fetchmany(chunk_size)
        if not rows:
            break
        for r in rows:
            count += 1
            line = "|".join(str(val) for val in r) + "\n"
            hasher.update(line.encode("utf-8"))
    return count, hasher.hexdigest()

def hash_project_state_records(cursor) -> Tuple[int, str, Dict[str, int]]:
    """Compute deterministic SHA256 over coverage_project_state."""
    sql = "SELECT project_name, data_version FROM coverage_project_state ORDER BY project_name"
    cursor.execute(sql)
    rows = cursor.fetchall()
    hasher = hashlib.sha256()
    states = {}
    for r in rows:
        pname, ver = str(r[0]), int(r[1])
        states[pname] = ver
        hasher.update(f"{pname}|{ver}\n".encode("utf-8"))
    return len(rows), hasher.hexdigest(), states

def hash_background_jobs_records(cursor) -> Tuple[int, str, Dict[str, int]]:
    """Compute status distribution and deterministic SHA256 over coverage_background_jobs using real schema."""
    sql = """
        SELECT job_id, project_name, COALESCE(kind, ''), COALESCE(state, ''), COALESCE(error_message, '')
        FROM coverage_background_jobs
        ORDER BY job_id
    """
    cursor.execute(sql)
    rows = cursor.fetchall()
    hasher = hashlib.sha256()
    dist = {}
    for r in rows:
        state = str(r[3])
        dist[state] = dist.get(state, 0) + 1
        line = "|".join(str(val) for val in r) + "\n"
        hasher.update(line.encode("utf-8"))
    return len(rows), hasher.hexdigest(), dist

def capture_database_snapshot(connection, release_identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Capture complete pre or post migration database snapshot with content hashes."""
    with connection.cursor() as cursor:
        analysis_count, analysis_hash = hash_analysis_records(cursor)
        index_count, index_hash = hash_line_index_records(cursor)
        proj_count, proj_hash, proj_versions = hash_project_state_records(cursor)
        job_count, job_hash, job_status_dist = hash_background_jobs_records(cursor)
        
    snapshot = {
        "captured_at": get_utc_iso(),
        "release_identity": release_identity or {},
        "tables": {
            "coverage_analysis": {
                "count": analysis_count,
                "content_hash": analysis_hash
            },
            "coverage_line_index": {
                "count": index_count,
                "content_hash": index_hash
            },
            "coverage_project_state": {
                "count": proj_count,
                "content_hash": proj_hash,
                "versions": proj_versions
            },
            "coverage_background_jobs": {
                "count": job_count,
                "content_hash": job_hash,
                "status_distribution": job_status_dist
            }
        }
    }
    return snapshot

def verify_data_integrity(pre_snapshot: Dict[str, Any], post_snapshot: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Verify that post migration snapshot matches pre migration snapshot according to zero-data-loss rules.
    Returns (is_valid, violation_reasons).
    """
    errors = []
    pre_tbls = pre_snapshot.get("tables", {})
    post_tbls = post_snapshot.get("tables", {})
    
    # 1. coverage_analysis checks
    pre_ana = pre_tbls.get("coverage_analysis", {})
    post_ana = post_tbls.get("coverage_analysis", {})
    if post_ana.get("count", 0) < pre_ana.get("count", 0):
        errors.append(f"Row count decreased in coverage_analysis: {pre_ana.get('count')} -> {post_ana.get('count')}")
    if post_ana.get("content_hash") != pre_ana.get("content_hash"):
        errors.append(f"Content hash mismatch in coverage_analysis: {pre_ana.get('content_hash')} != {post_ana.get('content_hash')}")
        
    # 2. coverage_line_index checks
    pre_idx = pre_tbls.get("coverage_line_index", {})
    post_idx = post_tbls.get("coverage_line_index", {})
    if post_idx.get("count", 0) < pre_idx.get("count", 0):
        errors.append(f"Row count decreased in coverage_line_index: {pre_idx.get('count')} -> {post_idx.get('count')}")
    if post_idx.get("content_hash") != pre_idx.get("content_hash"):
        errors.append(f"Content hash mismatch in coverage_line_index: {pre_idx.get('content_hash')} != {post_idx.get('content_hash')}")
        
    # 3. coverage_project_state checks
    pre_proj = pre_tbls.get("coverage_project_state", {})
    post_proj = post_tbls.get("coverage_project_state", {})
    pre_vers = pre_proj.get("versions", {})
    post_vers = post_proj.get("versions", {})
    for p, ver in pre_vers.items():
        if p not in post_vers:
            errors.append(f"Project state disappeared for project: '{p}'")
            
    is_valid = (len(errors) == 0)
    return is_valid, errors
