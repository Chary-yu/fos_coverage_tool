"""Legacy-to-VNext migration runner and semantic integrity snapshots."""

from __future__ import print_function

import hashlib
import json
import os
import sqlite3
import sys
from datetime import date, datetime, time
from decimal import Decimal

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.db.repositories import (
    AnalysisRepository,
    FileStateRepository,
    JobRepository,
    LineIndexRepository,
    ProjectRepository,
    ProjectStateRepository,
)
from app.db.repositories.base import adapt_sql, fetchall, fetchone, is_sqlite, insert_id
from app.db.transaction import transaction
from app.time_utils import utc_iso, utc_sql
from scripts.upgrade.database_identity import (
    assert_separate_connections, fingerprint_connection,
)


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS coverage_schema_meta (
    schema_key TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
    applied_at TEXT NOT NULL, release_sha TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS coverage_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
    scan_key TEXT NOT NULL UNIQUE, scan_type TEXT NOT NULL, review_scope TEXT NOT NULL,
    info_file_name TEXT NOT NULL DEFAULT '', info_sha256 TEXT NOT NULL DEFAULT '',
    imported_at TEXT NOT NULL, status TEXT NOT NULL, legacy_migrated INTEGER NOT NULL DEFAULT 0,
    metadata_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS coverage_scan_repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL,
    repository_name TEXT NOT NULL, repository_path TEXT NOT NULL DEFAULT '',
    branch_name TEXT NOT NULL DEFAULT '', old_commit_sha TEXT, new_commit_sha TEXT,
    verified INTEGER NOT NULL DEFAULT 0, captured_at TEXT NOT NULL,
    provenance TEXT NOT NULL DEFAULT '', UNIQUE(scan_id, repository_name)
);
CREATE TABLE IF NOT EXISTS coverage_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL,
    report_id TEXT NOT NULL UNIQUE, report_root TEXT NOT NULL DEFAULT '',
    source_signature TEXT NOT NULL DEFAULT '', sidecar_schema INTEGER NOT NULL DEFAULT 0,
    asset_identity TEXT NOT NULL DEFAULT '', generated_at TEXT
);
CREATE TABLE IF NOT EXISTS coverage_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL,
    repository_name TEXT NOT NULL DEFAULT '', file_path_hash TEXT NOT NULL,
    file_path TEXT NOT NULL, source_file_name TEXT NOT NULL DEFAULT '',
    UNIQUE(scan_id, repository_name, file_path_hash)
);
CREATE TABLE IF NOT EXISTS coverage_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT, file_id INTEGER NOT NULL,
    line_number INTEGER NOT NULL, line_text TEXT, coverage_state TEXT NOT NULL DEFAULT 'unknown',
    block_start_line INTEGER NOT NULL, block_end_line INTEGER NOT NULL,
    block_type TEXT NOT NULL DEFAULT 'single', function_name TEXT NOT NULL DEFAULT '',
    function_hash TEXT NOT NULL DEFAULT '', code_line_hash TEXT NOT NULL DEFAULT '',
    code_occurrence INTEGER NOT NULL DEFAULT 1, suggested_reviewer TEXT NOT NULL DEFAULT '',
    UNIQUE(file_id, line_number)
);
CREATE TABLE IF NOT EXISTS coverage_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT, line_id INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT '', is_draft INTEGER NOT NULL DEFAULT 0,
    reviewer TEXT NOT NULL DEFAULT '', coverage_method TEXT, uncovered_reason TEXT,
    comment TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_project_state (
    project_id INTEGER PRIMARY KEY, current_scan_id INTEGER,
    data_version INTEGER NOT NULL DEFAULT 0, file_state_version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_file_state (
    scan_id INTEGER NOT NULL, file_id INTEGER NOT NULL, total_lines INTEGER NOT NULL DEFAULT 0,
    total_uncovered INTEGER NOT NULL DEFAULT 0, filled_total INTEGER NOT NULL DEFAULT 0,
    draft_total INTEGER NOT NULL DEFAULT 0, confirmed_total INTEGER NOT NULL DEFAULT 0,
    pending_total INTEGER NOT NULL DEFAULT 0,
    ordinary_pending_total INTEGER NOT NULL DEFAULT 0,
    inherited_pending_total INTEGER NOT NULL DEFAULT 0,
    manual_draft_pending_total INTEGER NOT NULL DEFAULT 0,
    data_version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL, PRIMARY KEY(scan_id, file_id)
);
CREATE TABLE IF NOT EXISTS coverage_background_jobs (
    job_id TEXT PRIMARY KEY, project_id INTEGER, scan_id INTEGER, kind TEXT NOT NULL,
    state TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, input_payload TEXT NOT NULL,
    result_path TEXT NOT NULL DEFAULT '', error_message TEXT, data_version INTEGER,
    heartbeat_at TEXT, lease_owner TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_incremental_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL,
    report_id TEXT NOT NULL DEFAULT '', repository_name TEXT NOT NULL,
    old_commit_sha TEXT NOT NULL DEFAULT '', new_commit_sha TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL, generated_at TEXT NOT NULL,
    UNIQUE(scan_id, report_id, repository_name)
);
"""

# The SQLite test schema mirrors the additive MariaDB contract.  SQLite is
# used for fast deterministic tests, but it must not silently omit a Gate A/B/C
# field; otherwise a green unit suite could exercise a different domain model.
SQLITE_DOMAIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS coverage_schema_migrations (
    migration_id TEXT PRIMARY KEY, schema_key TEXT NOT NULL,
    from_version INTEGER NOT NULL DEFAULT 0, to_version INTEGER NOT NULL,
    ddl_sha256 TEXT NOT NULL, state TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, release_sha TEXT NOT NULL DEFAULT '',
    error_class TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS coverage_legacy_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT, migration_id TEXT NOT NULL,
    target_entity_type TEXT NOT NULL, target_entity_id INTEGER NOT NULL,
    source_table TEXT NOT NULL, source_identity TEXT NOT NULL,
    legacy_created_at TEXT, legacy_updated_at TEXT,
    legacy_raw_status TEXT, legacy_raw_is_draft INTEGER,
    raw_payload_sha256 TEXT NOT NULL, raw_payload TEXT, created_at TEXT NOT NULL,
    UNIQUE(migration_id, target_entity_type, target_entity_id, source_table)
);
CREATE TABLE IF NOT EXISTS coverage_repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
    repository_name TEXT NOT NULL, canonical_remote TEXT,
    last_observed_physical_path TEXT NOT NULL DEFAULT '',
    physical_resource_id INTEGER, lifecycle_state TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(project_id, repository_name)
);
CREATE TABLE IF NOT EXISTS coverage_repository_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL,
    repository_id INTEGER NOT NULL, alias_name TEXT NOT NULL,
    created_at TEXT NOT NULL, retired_at TEXT,
    UNIQUE(project_id, alias_name)
);
CREATE TABLE IF NOT EXISTS coverage_repository_resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT, resource_key TEXT NOT NULL UNIQUE,
    resolved_git_common_dir TEXT NOT NULL, resolved_worktree_root TEXT NOT NULL,
    fs_device INTEGER, fs_inode INTEGER, next_fencing_token INTEGER NOT NULL DEFAULT 0,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_analysis_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT, conclusion_status TEXT NOT NULL DEFAULT '',
    coverage_method TEXT, uncovered_reason TEXT, comment TEXT,
    content_revision INTEGER NOT NULL DEFAULT 1, content_hash TEXT NOT NULL,
    content_origin TEXT NOT NULL DEFAULT 'MANUAL', legacy_source_analysis_id INTEGER,
    legacy_source_created_at TEXT, legacy_source_updated_at TEXT,
    legacy_raw_status TEXT, legacy_raw_is_draft INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_analysis_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL,
    repository_id INTEGER, file_id INTEGER NOT NULL, start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL, origin TEXT NOT NULL DEFAULT 'MANUAL',
    block_identity_verified INTEGER NOT NULL DEFAULT 0, originating_record_id INTEGER,
    initial_content_hash TEXT, created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_inheritance_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT, decision_run_id TEXT NOT NULL,
    candidate_scan_id INTEGER NOT NULL, source_scan_id INTEGER NOT NULL,
    source_analysis_block_id INTEGER NOT NULL, repository_id INTEGER NOT NULL,
    candidate_file_id INTEGER NOT NULL, mapping_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(decision_run_id, source_analysis_block_id, candidate_file_id, mapping_fingerprint)
);
CREATE TABLE IF NOT EXISTS coverage_analysis_line_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL,
    line_id INTEGER NOT NULL, analysis_record_id INTEGER NOT NULL,
    analysis_block_id INTEGER, review_state TEXT NOT NULL, relation_origin TEXT NOT NULL,
    inheritance_group_id INTEGER, is_active INTEGER NOT NULL DEFAULT 1,
    reviewed_by TEXT NOT NULL DEFAULT '', reviewed_at TEXT,
    source_scan_id INTEGER, source_line_id INTEGER, source_relation_id INTEGER,
    relation_revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, UNIQUE(scan_id, line_id)
);
CREATE TABLE IF NOT EXISTS coverage_inheritance_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, decision_run_id TEXT NOT NULL,
    candidate_scan_id INTEGER NOT NULL, candidate_line_id INTEGER NOT NULL,
    source_scan_id INTEGER, source_line_id INTEGER, source_relation_id INTEGER,
    decision TEXT NOT NULL, reason_code TEXT NOT NULL, algorithm_version TEXT NOT NULL,
    old_commit_sha TEXT, new_commit_sha TEXT,
    line_mapping_fingerprint TEXT NOT NULL DEFAULT '',
    function_identity_fingerprint TEXT NOT NULL DEFAULT '',
    control_context_fingerprint TEXT NOT NULL DEFAULT '',
    preprocessor_context_fingerprint TEXT NOT NULL DEFAULT '',
    dependency_fingerprint TEXT NOT NULL DEFAULT '', evaluated_at TEXT NOT NULL,
    UNIQUE(decision_run_id, candidate_line_id)
);
CREATE TABLE IF NOT EXISTS coverage_inheritance_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL, line_id INTEGER NOT NULL,
    rejected_relation_id INTEGER NOT NULL, rejected_relation_revision INTEGER NOT NULL,
    rejected_analysis_record_id INTEGER NOT NULL, rejected_source_scan_id INTEGER,
    rejected_source_line_id INTEGER, rejected_source_relation_id INTEGER,
    rejection_revision INTEGER NOT NULL DEFAULT 1, is_active INTEGER NOT NULL DEFAULT 1,
    terminal_reason TEXT, rejected_by TEXT NOT NULL, rejected_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS coverage_repository_resource_locks (
    physical_resource_id INTEGER PRIMARY KEY, job_id TEXT NOT NULL,
    owner_token TEXT NOT NULL, fencing_token INTEGER NOT NULL,
    heartbeat_at TEXT NOT NULL, acquired_at TEXT NOT NULL, expires_at TEXT,
    UNIQUE(job_id, physical_resource_id)
);
CREATE TABLE IF NOT EXISTS coverage_import_artifacts (
    artifact_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL,
    staged_path TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL DEFAULT 0,
    immutable INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
    UNIQUE(job_id, kind)
);
CREATE TABLE IF NOT EXISTS coverage_import_checkpoints (
    job_id TEXT PRIMARY KEY, scan_id INTEGER, phase TEXT NOT NULL,
    phase_version INTEGER NOT NULL DEFAULT 1, checkpoint_seq INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL, input_sha256 TEXT NOT NULL DEFAULT '',
    fencing_token INTEGER NOT NULL DEFAULT 0, expected_current_scan_id INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_import_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, scan_id INTEGER,
    phase TEXT NOT NULL, error_class TEXT NOT NULL, error_fingerprint TEXT NOT NULL,
    message_redacted TEXT, fencing_token INTEGER, occurred_at TEXT NOT NULL,
    UNIQUE(job_id, phase, error_fingerprint)
);
"""


SQLITE_ADDITIVE_COLUMNS = {
    "coverage_schema_meta": [
        ("migration_id", "TEXT NOT NULL DEFAULT ''"),
    ],
    "coverage_scans": [
        ("predecessor_scan_id", "INTEGER"),
        ("algorithm_version", "TEXT NOT NULL DEFAULT ''"),
    ],
    "coverage_scan_repositories": [
        ("repository_id", "INTEGER"),
        ("commit_sha", "TEXT"),
        ("identity_verified", "INTEGER NOT NULL DEFAULT 0"),
        ("identity_provenance", "TEXT NOT NULL DEFAULT ''"),
    ],
    "coverage_background_jobs": [
        ("handler_version", "TEXT NOT NULL DEFAULT ''"),
        ("legacy_raw_percent", "REAL"),
        ("legacy_percent_unit", "TEXT NOT NULL DEFAULT ''"),
    ],
    "coverage_file_state": [
        ("ordinary_pending_total", "INTEGER NOT NULL DEFAULT 0"),
        ("inherited_pending_total", "INTEGER NOT NULL DEFAULT 0"),
        ("manual_draft_pending_total", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _now():
    return utc_sql()


def _table_exists(connection, table_name):
    if is_sqlite(connection):
        row = fetchone(connection, """
            SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?
        """, (table_name,))
        return bool(row)
    row = fetchone(connection, """
        SELECT TABLE_NAME FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ?
    """, (table_name,))
    return bool(row)


def _column_exists(connection, table_name, column_name):
    if is_sqlite(connection):
        rows = connection.execute("PRAGMA table_info({})".format(table_name)).fetchall()
        return any(str(row[1]) == column_name for row in rows)
    row = fetchone(connection, """
        SELECT COLUMN_NAME FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND COLUMN_NAME = ?
    """, (table_name, column_name))
    return bool(row)


def _rows(connection, table_name):
    if not _table_exists(connection, table_name):
        return []
    return fetchall(connection, "SELECT * FROM {}".format(table_name))


def _project_names(connection):
    names = set()
    for table in ("coverage_analysis", "coverage_line_index",
                  "coverage_project_state", "coverage_background_jobs"):
        for row in _rows(connection, table):
            name = row.get("project_name")
            if name:
                names.add(str(name))
    return sorted(names)


def _legacy_key(row):
    path = str(row.get("file_path") or "")
    file_hash = str(row.get("file_path_hash") or "")
    if not file_hash:
        file_hash = hashlib.md5(path.encode("utf-8")).hexdigest()
    return file_hash, int(row.get("line_number") or 0), path


def _legacy_text(row, names):
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _legacy_payload_hash(row):
    payload = json.dumps(dict(row or {}), ensure_ascii=False, sort_keys=True,
                         default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _nullable_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def capture_legacy_snapshot(connection):
    """Normalize legacy facts without changing their source semantics."""
    analysis = []
    for row in _rows(connection, "coverage_analysis"):
        file_hash, line_number, path = _legacy_key(row)
        analysis.append({
            "source_pk": _nullable_int(row.get("id")),
            "project_name": str(row.get("project_name") or ""),
            "file_path_hash": file_hash,
            "file_path": path,
            "line_number": line_number,
            "status": str(row.get("status") or ""),
            "is_draft": int(row.get("is_draft") or 0),
            "reviewer": str(row.get("reviewer") or ""),
            "coverage_method": _legacy_text(row, ("coverage_method", "method")),
            "uncovered_reason": _legacy_text(row, ("uncovered_reason", "reason")),
            "comment": _legacy_text(row, ("comment", "comments", "remark")),
            "legacy_created_at": row.get("created_at"),
            "legacy_updated_at": row.get("updated_at"),
            "raw_payload_sha256": _legacy_payload_hash(row),
        })
    lines = []
    for row in _rows(connection, "coverage_line_index"):
        file_hash, line_number, path = _legacy_key(row)
        lines.append({
            "source_pk": _nullable_int(row.get("id")),
            "project_name": str(row.get("project_name") or ""),
            "file_path_hash": file_hash,
            "file_path": path,
            "line_number": line_number,
            "line_text": str(row.get("line_text") or ""),
            "block_start_line": int(row.get("block_start_line") or line_number),
            "block_end_line": int(row.get("block_end_line") or line_number),
            "block_type": str(row.get("block_type") or "single"),
            "function_name": str(row.get("function_name") or ""),
            "function_hash": str(row.get("function_hash") or ""),
            "code_line_hash": str(row.get("code_line_hash") or ""),
            "code_occurrence": int(row.get("code_occurrence") or 1),
            "legacy_created_at": row.get("created_at"),
            "legacy_updated_at": row.get("updated_at"),
            "raw_payload_sha256": _legacy_payload_hash(row),
        })
    projects = {}
    project_metadata = {}
    for row in _rows(connection, "coverage_project_state"):
        name = str(row.get("project_name") or "")
        projects[name] = int(row.get("data_version") or 0)
        project_metadata[name] = {
            "file_state_version": int(row.get("file_state_version") or 0),
            "current_scan_key": str(row.get("current_scan_key") or ""),
            "updated_at": row.get("updated_at"),
            "raw_payload_sha256": _legacy_payload_hash(row),
        }
    jobs = []
    for row in _rows(connection, "coverage_background_jobs"):
        jobs.append({
            "job_id": str(row.get("job_id") or ""),
            "project_name": str(row.get("project_name") or ""),
            "kind": str(row.get("kind") or ""),
            "state": str(row.get("state") or ""),
            "data_version": int(row.get("data_version") or 0),
            "error_message": str(row.get("error_message") or ""),
            "legacy_raw_percent": row.get("percent"),
            "legacy_percent_unit": str(row.get("progress_unit") or ""),
            "stage": str(row.get("stage") or ""),
            "message": str(row.get("message") or ""),
            "input_payload": row.get("input_payload"),
            "result_path": str(row.get("result_path") or ""),
            "filename": str(row.get("filename") or ""),
            "row_count": _nullable_int(row.get("row_count")),
            "heartbeat_at": row.get("heartbeat_at"),
            "finished_at": row.get("finished_at"),
            "created_at": row.get("created_at"),
            "started_at": row.get("started_at"),
            "updated_at": row.get("updated_at"),
            "raw_payload_sha256": _legacy_payload_hash(row),
        })
    return {
        "projects": sorted(name for name in _project_names(connection) if name),
        "project_data_versions": dict(sorted(projects.items())),
        "project_metadata": project_metadata,
        "lines": sorted(lines, key=lambda item: (
            item["project_name"], item["file_path_hash"], item["line_number"])),
        "analyses": sorted(analysis, key=lambda item: (
            item["project_name"], item["file_path_hash"], item["line_number"])),
        "jobs": sorted(jobs, key=lambda item: item["job_id"]),
    }


def capture_vnext_snapshot(connection):
    projects = fetchall(connection, "SELECT id, project_name FROM coverage_projects ORDER BY project_name")
    project_ids = {row["id"]: row["project_name"] for row in projects}
    scans = fetchall(connection, """
        SELECT id, project_id, scan_key, scan_type, legacy_migrated
        FROM coverage_scans ORDER BY id
    """)
    lines = fetchall(connection, """
        SELECT p.project_name, f.file_path_hash, f.file_path, l.line_number,
               l.line_text, l.block_start_line, l.block_end_line, l.block_type,
               l.function_name, l.function_hash, l.code_line_hash, l.code_occurrence
        FROM coverage_lines l
        JOIN coverage_files f ON f.id = l.file_id
        JOIN coverage_scans s ON s.id = f.scan_id
        JOIN coverage_projects p ON p.id = s.project_id
        ORDER BY p.project_name, f.file_path_hash, l.line_number
    """)
    analyses = fetchall(connection, """
        SELECT p.project_name, f.file_path_hash, f.file_path, l.line_number,
               a.status, a.is_draft, a.reviewer, a.coverage_method,
               a.uncovered_reason, a.comment
        FROM coverage_analyses a
        JOIN coverage_lines l ON l.id = a.line_id
        JOIN coverage_files f ON f.id = l.file_id
        JOIN coverage_scans s ON s.id = f.scan_id
        JOIN coverage_projects p ON p.id = s.project_id
        ORDER BY p.project_name, f.file_path_hash, l.line_number
    """)
    states = fetchall(connection, """
        SELECT p.project_name, s.data_version
        FROM coverage_project_state s JOIN coverage_projects p ON p.id = s.project_id
        ORDER BY p.project_name
    """)
    jobs = fetchall(connection, """
        SELECT job_id, kind, state, error_message, data_version
        FROM coverage_background_jobs ORDER BY job_id
    """)
    return {
        "projects": sorted(project_ids.values()),
        "scans": scans,
        "lines": lines,
        "analyses": analyses,
        "project_data_versions": {
            row["project_name"]: int(row["data_version"]) for row in states
        },
        "jobs": jobs,
    }


def capture_vnext_semantic_snapshot(connection):
    """Normalize VNext facts to the legacy business-fact shape for hashing."""
    snapshot = capture_vnext_snapshot(connection)
    jobs = fetchall(connection, """
        SELECT j.job_id, p.project_name, j.kind, j.state,
               COALESCE(j.data_version, 0) AS data_version,
               COALESCE(j.error_message, '') AS error_message
        FROM coverage_background_jobs j
        LEFT JOIN coverage_projects p ON p.id = j.project_id
        ORDER BY j.job_id
    """)
    normalized_jobs = []
    for job in jobs:
        item = dict(job)
        if item.get("state") in ("queued", "running", "interrupted"):
            item["state"] = "interrupted"
            item["error_message"] = item.get("error_message") or (
                "legacy active job requires manual migration decision"
            )
        normalized_jobs.append(item)
    provenance = []
    if _table_exists(connection, "coverage_legacy_provenance"):
        provenance_rows = fetchall(connection, """
            SELECT target_entity_type, source_table, source_identity,
                   legacy_created_at,
                   legacy_updated_at, legacy_raw_status, legacy_raw_is_draft,
                   raw_payload_sha256
            FROM coverage_legacy_provenance
            ORDER BY source_table, source_identity, target_entity_type
        """)
        provenance = []
        for row in provenance_rows:
            item = dict(row)
            if item.get("legacy_raw_status") == "":
                item["legacy_raw_status"] = None
            provenance.append(item)
    return {
        "projects": snapshot["projects"],
        "project_data_versions": snapshot["project_data_versions"],
        "lines": snapshot["lines"],
        "analyses": snapshot["analyses"],
        "jobs": normalized_jobs,
        "legacy_provenance": provenance,
    }


def capture_legacy_semantic_snapshot(connection, snapshot=None):
    """Normalize expected migration transformations for semantic comparison."""
    source_snapshot = snapshot if snapshot is not None else capture_legacy_snapshot(connection)
    raw_snapshot = {
        key: (list(value) if isinstance(value, list) else dict(value)
              if isinstance(value, dict) else value)
        for key, value in source_snapshot.items()
    }
    # Semantic normalization is allowed to add analysis-only line facts and
    # rewrite blank historical paths, but it must never mutate the raw input
    # that the migration itself will consume.  Reusing the already captured
    # snapshot avoids a second database read/JSON normalization pass.
    snapshot = {}
    for key, value in source_snapshot.items():
        if isinstance(value, list):
            snapshot[key] = [dict(item) if isinstance(item, dict) else item
                             for item in value]
        elif isinstance(value, dict):
            snapshot[key] = {
                item_key: (dict(item_value)
                           if isinstance(item_value, dict) else item_value)
                for item_key, item_value in value.items()
            }
        else:
            snapshot[key] = value
    # file_state/current-scan metadata is operational provenance; data_version
    # below is the only project state fact in the authoritative semantic hash.
    snapshot.pop("project_metadata", None)
    line_keys = {
        (row["project_name"], row["file_path_hash"], row["file_path"], row["line_number"])
        for row in snapshot["lines"]
    }
    # Some historical rows carried only file_path_hash.  The migration
    # resolves a blank analysis path against a unique line-index path (or the
    # hash when the path is ambiguous/missing), so the expected semantic
    # snapshot must apply that same deterministic identity rule.  Raw source
    # payload hashes stay untouched in ``raw_snapshot`` for provenance.
    line_paths = {}
    for line in snapshot["lines"]:
        key = (line["project_name"], line["file_path_hash"], line["line_number"])
        if line.get("file_path"):
            line_paths.setdefault(key, set()).add(line["file_path"])
        elif not line.get("file_path"):
            line["file_path"] = line["file_path_hash"]
    for analysis in snapshot["analyses"]:
        if analysis.get("file_path"):
            continue
        key = (analysis["project_name"], analysis["file_path_hash"],
               analysis["line_number"])
        candidates = line_paths.get(key, set())
        analysis["file_path"] = next(iter(candidates)) if len(candidates) == 1 else (
            analysis["file_path_hash"]
        )
    line_keys = {
        (row["project_name"], row["file_path_hash"], row["file_path"], row["line_number"])
        for row in snapshot["lines"]
    }
    for analysis in snapshot["analyses"]:
        key = (
            analysis["project_name"], analysis["file_path_hash"],
            analysis["file_path"], analysis["line_number"],
        )
        if key not in line_keys:
            snapshot["lines"].append({
                "project_name": analysis["project_name"],
                "file_path_hash": analysis["file_path_hash"],
                "file_path": analysis["file_path"],
                "line_number": analysis["line_number"],
                "line_text": "",
                "block_start_line": analysis["line_number"],
                "block_end_line": analysis["line_number"],
                "block_type": "unknown",
                "function_name": "",
                "function_hash": "",
                "code_line_hash": "",
                "code_occurrence": 1,
            })
    snapshot["lines"].sort(key=lambda item: (
        item["project_name"], item["file_path_hash"], item["line_number"]
    ))
    jobs = []
    for job in snapshot["jobs"]:
        item = dict(job)
        if item.get("state") in ("queued", "running", "interrupted"):
            item["state"] = "interrupted"
            item["error_message"] = item.get("error_message") or (
                "legacy active job requires manual migration decision"
            )
        jobs.append({key: item.get(key) for key in (
            "job_id", "project_name", "kind", "state", "data_version",
            "error_message",
        )})
    snapshot["jobs"] = jobs
    # Surrogate source IDs and raw payloads are provenance, not business
    # identity.  Keep timestamps/status facts in a separate deterministic
    # list so target IDs do not affect the authoritative semantic hash.
    snapshot["analyses"] = [
        {key: row.get(key) for key in (
            "project_name", "file_path_hash", "file_path", "line_number",
            "status", "is_draft", "reviewer", "coverage_method",
            "uncovered_reason", "comment",
        )}
        for row in snapshot["analyses"]
    ]
    snapshot["lines"] = [
        {key: row.get(key) for key in (
            "project_name", "file_path_hash", "file_path", "line_number",
            "line_text", "block_start_line", "block_end_line", "block_type",
            "function_name", "function_hash", "code_line_hash", "code_occurrence",
        )}
        for row in snapshot["lines"]
    ]
    provenance = []
    for row in raw_snapshot["lines"]:
        provenance.append({
            "target_entity_type": "line",
            "source_table": "coverage_line_index",
            "source_identity": "{}:{}:{}".format(
                row.get("project_name") or "", row.get("file_path_hash") or "",
                row.get("line_number") or 0,
            ),
            "legacy_created_at": row.get("legacy_created_at"),
            "legacy_updated_at": row.get("legacy_updated_at"),
            "legacy_raw_status": None,
            "legacy_raw_is_draft": None,
            "raw_payload_sha256": row.get("raw_payload_sha256") or "",
        })
    for row in raw_snapshot["analyses"]:
        provenance.append({
            "target_entity_type": "legacy_analysis",
            "source_table": "coverage_analysis",
            "source_identity": "{}:{}:{}".format(
                row.get("project_name") or "", row.get("file_path_hash") or "",
                row.get("line_number") or 0,
            ),
            "legacy_created_at": row.get("legacy_created_at"),
            "legacy_updated_at": row.get("legacy_updated_at"),
            "legacy_raw_status": row.get("status"),
            "legacy_raw_is_draft": row.get("is_draft"),
            "raw_payload_sha256": row.get("raw_payload_sha256") or "",
        })
    for project_name, metadata in sorted(raw_snapshot.get("project_metadata", {}).items()):
        provenance.append({
            "target_entity_type": "project_state",
            "source_table": "coverage_project_state",
            "source_identity": project_name,
            "legacy_created_at": None,
            "legacy_updated_at": metadata.get("updated_at"),
            "legacy_raw_status": None,
            "legacy_raw_is_draft": None,
            "raw_payload_sha256": metadata.get("raw_payload_sha256") or "",
        })
    for row in raw_snapshot["jobs"]:
        provenance.append({
            "target_entity_type": "job",
            "source_table": "coverage_background_jobs",
            "source_identity": row.get("job_id") or "",
            "legacy_created_at": row.get("created_at"),
            "legacy_updated_at": row.get("updated_at"),
            "legacy_raw_status": row.get("state"),
            "legacy_raw_is_draft": None,
            "raw_payload_sha256": row.get("raw_payload_sha256") or "",
        })
    snapshot["legacy_provenance"] = sorted(
        provenance,
        key=lambda item: (item["source_table"], item["source_identity"]),
    )
    return snapshot


def _semantic_json_default(value):
    """Normalize DB-API scalar values without losing semantic facts.

    SQLite exposes historical timestamps as strings while PyMySQL exposes
    DATETIME/DECIMAL values as Python objects.  Using the SQL textual form
    keeps hashes comparable across both drivers and remains deterministic.
    Unsupported objects fail closed instead of being silently stringified.
    """
    if isinstance(value, (datetime, date, time, Decimal)):
        return str(value)
    raise TypeError("Object of type {} is not JSON serializable".format(
        type(value).__name__
    ))


def semantic_hash(snapshot):
    payload = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=_semantic_json_default,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _upsert_legacy_provenance(connection, migration_id, entity_type,
                               target_entity_id, source_table, source_identity,
                               row, raw_payload=None):
    """Persist source timestamps/status/hash in the target DB idempotently."""
    values = (
        migration_id, entity_type, int(target_entity_id or 0), source_table,
        str(source_identity or ""), row.get("legacy_created_at"),
        row.get("legacy_updated_at"), row.get("status"),
        row.get("is_draft"), row.get("raw_payload_sha256") or "",
        raw_payload, _now(),
    )
    existing = fetchone(connection, """
        SELECT id, raw_payload_sha256 FROM coverage_legacy_provenance
        WHERE migration_id=? AND target_entity_type=? AND target_entity_id=?
          AND source_table=?
    """, values[:4])
    if existing:
        if str(existing.get("raw_payload_sha256") or "") != values[9]:
            raise ValueError("legacy provenance input changed on idempotent rerun")
        return int(existing.get("id") or 0)
    cursor = connection.cursor()
    cursor.execute(adapt_sql(connection, """
        INSERT INTO coverage_legacy_provenance(
            migration_id, target_entity_type, target_entity_id, source_table,
            source_identity, legacy_created_at, legacy_updated_at,
            legacy_raw_status, legacy_raw_is_draft, raw_payload_sha256,
            raw_payload, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """), values)
    result = insert_id(cursor)
    cursor.close()
    return result


def _upsert_legacy_provenance_many(connection, migration_id, records):
    """Persist provenance in bounded batches without losing idempotency.

    The first migration implementation called ``_upsert_legacy_provenance``
    once per line and once per analysis.  That made a production-sized
    rehearsal perform a SELECT/INSERT round trip for every fact even though
    the target transaction was already authoritative.  Resolve the existing
    identities once, validate their immutable hashes, and insert only missing
    rows with ``executemany``.
    """
    normalized = {}
    for item in records or []:
        values = dict(item or {})
        key = (
            str(values.get("entity_type") or ""),
            int(values.get("target_entity_id") or 0),
            str(values.get("source_table") or ""),
        )
        if not key[0] or not key[1] or not key[2]:
            raise ValueError("legacy provenance identity is incomplete")
        digest = str(values.get("raw_payload_sha256") or "")
        if not digest:
            raise ValueError("legacy provenance raw payload hash is required")
        previous = normalized.get(key)
        if previous and previous.get("raw_payload_sha256") != digest:
            raise ValueError("duplicate legacy provenance identity has changed input")
        normalized[key] = values
    if not normalized:
        return {"inserted": 0, "existing": 0}

    existing_rows = fetchall(connection, """
        SELECT target_entity_type, target_entity_id, source_table,
               raw_payload_sha256
        FROM coverage_legacy_provenance
        WHERE migration_id=?
    """, (migration_id,))
    existing = {
        (str(row.get("target_entity_type") or ""),
         int(row.get("target_entity_id") or 0),
         str(row.get("source_table") or "")): str(
             row.get("raw_payload_sha256") or ""
         )
        for row in existing_rows
    }
    inserts = []
    existing_count = 0
    for key, item in sorted(normalized.items()):
        digest = str(item.get("raw_payload_sha256") or "")
        if key in existing:
            existing_count += 1
            if existing[key] != digest:
                raise ValueError("legacy provenance input changed on idempotent rerun")
            continue
        inserts.append((
            migration_id, key[0], key[1], key[2],
            str(item.get("source_identity") or ""),
            item.get("legacy_created_at"), item.get("legacy_updated_at"),
            item.get("legacy_raw_status"), item.get("legacy_raw_is_draft"),
            digest, item.get("raw_payload"), _now(),
        ))
    if inserts:
        cursor = connection.cursor()
        try:
            cursor.executemany(adapt_sql(connection, """
                INSERT INTO coverage_legacy_provenance(
                    migration_id, target_entity_type, target_entity_id,
                    source_table, source_identity, legacy_created_at,
                    legacy_updated_at, legacy_raw_status, legacy_raw_is_draft,
                    raw_payload_sha256, raw_payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """), inserts)
        finally:
            cursor.close()
    return {"inserted": len(inserts), "existing": existing_count}


def create_sqlite_schema(connection):
    connection.executescript(SQLITE_SCHEMA)
    for table_name, columns in SQLITE_ADDITIVE_COLUMNS.items():
        existing = {
            str(row[1]) for row in connection.execute(
                "PRAGMA table_info({})".format(table_name)
            ).fetchall()
        }
        for column_name, definition in columns:
            if column_name not in existing:
                connection.execute(
                    "ALTER TABLE {} ADD COLUMN {} {}".format(
                        table_name, column_name, definition
                    )
                )
    connection.executescript(SQLITE_DOMAIN_SCHEMA)
    connection.commit()


def _split_sql(sql_text):
    """Split DDL on semicolons while respecting strings and SQL comments.

    MariaDB receives each returned statement directly. Keeping a leading
    ``--``/``/*`` comment attached to the following DDL makes the server
    parse the comment and statement as one malformed command, so comments
    must be removed before statements are sent to the driver.
    """
    statements = []
    current = []
    quote = None
    line_comment = False
    block_comment = False
    index = 0
    length = len(sql_text or "")
    while index < length:
        char = sql_text[index]
        next_char = sql_text[index + 1] if index + 1 < length else ""
        if line_comment:
            if char in ("\n", "\r"):
                line_comment = False
                current.append("\n")
            index += 1
            continue
        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote is None:
            if char == "#":
                line_comment = True
                index += 1
                continue
            if char == "-" and next_char == "-" and (
                    index + 2 >= length or sql_text[index + 2].isspace()):
                line_comment = True
                index += 2
                continue
            if char == "/" and next_char == "*":
                block_comment = True
                index += 2
                continue
            if char in ("'", '"', chr(96)):
                quote = char
            if char == ";":
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
                index += 1
                continue
        else:
            # SQL strings escape a quote by doubling it. Do not terminate the
            # quoted state on the first half of ``''``/``""``/````.
            if char == quote:
                if next_char == quote:
                    current.append(char)
                    current.append(next_char)
                    index += 2
                    continue
                quote = None
        current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def apply_schema(connection, ddl_path, release_sha=""):
    with open(ddl_path, "r", encoding="utf-8") as stream:
        ddl = stream.read()
    ddl_sha256 = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    migration_id = "coverage-vnext-core-v2"

    if is_sqlite(connection):
        create_sqlite_schema(connection)
        existing = fetchone(connection, """
            SELECT * FROM coverage_schema_migrations WHERE migration_id=?
        """, (migration_id,))
        if existing and str(existing.get("state") or "") == "APPLIED":
            if str(existing.get("ddl_sha256") or "") != ddl_sha256:
                raise ValueError("schema migration checksum changed after APPLIED state")
            return {"status": "PASSED", "migration_id": migration_id,
                    "idempotent": True, "ddl_sha256": ddl_sha256}
        now = _now()
        cursor = connection.cursor()
        if existing:
            cursor.execute("""
                UPDATE coverage_schema_migrations
                SET state='APPLIED', ddl_sha256=?, to_version=1,
                    finished_at=?, release_sha=?, error_class=''
                WHERE migration_id=?
            """, (ddl_sha256, now, release_sha or "", migration_id))
        else:
            cursor.execute("""
                INSERT INTO coverage_schema_migrations(
                    migration_id, schema_key, from_version, to_version,
                    ddl_sha256, state, started_at, finished_at, release_sha
                ) VALUES (?, ?, 0, 1, ?, 'APPLIED', ?, ?, ?)
            """, (migration_id, "coverage_vnext_core", ddl_sha256,
                    now, now, release_sha or ""))
        for schema_key, version in (
                ("coverage_vnext_core", 1), ("coverage_analysis_domain", 0),
                ("coverage_inheritance", 0), ("coverage_vnext", 1)):
            if fetchone(connection, "SELECT schema_key FROM coverage_schema_meta WHERE schema_key=?",
                         (schema_key,)):
                cursor.execute("""
                    UPDATE coverage_schema_meta
                    SET schema_version=?, applied_at=?, release_sha=?, migration_id=?
                    WHERE schema_key=?
                """, (version, now, release_sha or "", migration_id, schema_key))
            else:
                cursor.execute("""
                    INSERT INTO coverage_schema_meta(
                        schema_key, schema_version, applied_at, release_sha, migration_id
                    ) VALUES (?, ?, ?, ?, ?)
                """, (schema_key, version, now, release_sha or "", migration_id))
        cursor.close()
        connection.commit()
        return {"status": "PASSED", "migration_id": migration_id,
                "idempotent": bool(existing), "ddl_sha256": ddl_sha256}

    # The ledger itself must exist before the first non-transactional MariaDB
    # DDL statement.  It is intentionally additive and safe on a partially
    # upgraded target.
    ledger_ddl = """
        CREATE TABLE IF NOT EXISTS coverage_schema_migrations (
            migration_id VARCHAR(128) NOT NULL,
            schema_key VARCHAR(64) NOT NULL,
            from_version INT NOT NULL DEFAULT 0,
            to_version INT NOT NULL,
            ddl_sha256 CHAR(64) NOT NULL,
            state VARCHAR(16) NOT NULL,
            started_at DATETIME NOT NULL,
            finished_at DATETIME NULL,
            release_sha CHAR(40) NOT NULL DEFAULT '',
            error_class VARCHAR(128) NOT NULL DEFAULT '',
            PRIMARY KEY (migration_id)
        )
    """
    cursor = connection.cursor()
    cursor.execute(adapt_sql(connection, ledger_ddl))
    cursor.close()
    connection.commit()
    # ``coverage_schema_meta`` is part of the same additive core schema, but
    # the migration ledger needs it before the full DDL loop so it can derive
    # ``from_version`` on a genuinely empty MariaDB target.  SQLite creates
    # this table in ``create_sqlite_schema``; MariaDB must bootstrap it here.
    meta_ddl = """
        CREATE TABLE IF NOT EXISTS coverage_schema_meta (
            schema_key VARCHAR(64) NOT NULL,
            schema_version INT NOT NULL,
            applied_at DATETIME(6) NOT NULL,
            release_sha CHAR(40) NOT NULL DEFAULT '',
            migration_id VARCHAR(128) NOT NULL DEFAULT '',
            PRIMARY KEY (schema_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """
    cursor = connection.cursor()
    cursor.execute(adapt_sql(connection, meta_ddl))
    cursor.close()
    connection.commit()
    existing_migration = fetchone(connection, """
        SELECT * FROM coverage_schema_migrations WHERE migration_id = ?
    """, (migration_id,))
    if existing_migration and str(existing_migration.get("state") or "") == "APPLIED":
        if str(existing_migration.get("ddl_sha256") or "") != ddl_sha256:
            raise ValueError("schema migration checksum changed after APPLIED state")
        return {"status": "PASSED", "migration_id": migration_id,
                "idempotent": True, "ddl_sha256": ddl_sha256}

    schema_version_row = fetchone(connection, """
        SELECT schema_version FROM coverage_schema_meta WHERE schema_key = ?
    """, ("coverage_vnext_core",))
    from_version = int((schema_version_row or {}).get("schema_version") or 0)
    now = _now()
    if existing_migration:
        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, """
            UPDATE coverage_schema_migrations
            SET schema_key=?, from_version=?, to_version=?, ddl_sha256=?, state='STARTED',
                started_at=?, finished_at=NULL, release_sha=?, error_class=''
            WHERE migration_id=?
        """), ("coverage_vnext_core", from_version, 1, ddl_sha256, now,
                release_sha or "", migration_id))
        cursor.close()
    else:
        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, """
            INSERT INTO coverage_schema_migrations(
                migration_id, schema_key, from_version, to_version, ddl_sha256,
                state, started_at, release_sha
            ) VALUES (?, ?, ?, ?, ?, 'STARTED', ?, ?)
        """), (migration_id, "coverage_vnext_core", from_version, 1,
                ddl_sha256, now, release_sha or ""))
        cursor.close()
    connection.commit()
    try:
        for statement in _split_sql(ddl):
            cursor = connection.cursor()
            cursor.execute(adapt_sql(connection, statement))
            cursor.close()
        # Existing Candidate databases are upgraded through information_schema
        # checks rather than unsafe ADD COLUMN IF NOT EXISTS (unsupported by
        # MariaDB 5.5).  Fresh targets get the columns from the DDL above.
        additions = {
            "coverage_schema_meta": [
                ("migration_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ],
            "coverage_scans": [
                ("predecessor_scan_id", "BIGINT NULL"),
                ("algorithm_version", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ],
            "coverage_scan_repositories": [
                ("repository_id", "BIGINT NULL"),
                ("commit_sha", "CHAR(40) NULL"),
                ("identity_verified", "TINYINT NOT NULL DEFAULT 0"),
                ("identity_provenance", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ],
            "coverage_background_jobs": [
                ("lease_owner", "VARCHAR(128) NOT NULL DEFAULT ''"),
                ("handler_version", "VARCHAR(64) NOT NULL DEFAULT ''"),
                ("legacy_raw_percent", "DECIMAL(12,3) NULL"),
                ("legacy_percent_unit", "VARCHAR(32) NOT NULL DEFAULT ''"),
            ],
            "coverage_file_state": [
                ("ordinary_pending_total", "INT NOT NULL DEFAULT 0"),
                ("inherited_pending_total", "INT NOT NULL DEFAULT 0"),
                ("manual_draft_pending_total", "INT NOT NULL DEFAULT 0"),
            ],
        }
        for table_name, columns in additions.items():
            if not _table_exists(connection, table_name):
                continue
            for column_name, definition in columns:
                if _column_exists(connection, table_name, column_name):
                    continue
                cursor = connection.cursor()
                cursor.execute(adapt_sql(connection, "ALTER TABLE {} ADD COLUMN {} {}".format(
                    table_name, column_name, definition
                )))
                cursor.close()
        # Keep the historical key for old health checks while introducing the
        # stage-specific metadata required by Gate A.
        meta_rows = [
            ("coverage_vnext_core", 1, migration_id),
            ("coverage_analysis_domain", 0, migration_id),
            ("coverage_inheritance", 0, migration_id),
            ("coverage_vnext", 1, migration_id),
        ]
        for schema_key, version, meta_migration in meta_rows:
            existing = fetchone(connection, "SELECT schema_key FROM coverage_schema_meta WHERE schema_key=?",
                                 (schema_key,))
            cursor = connection.cursor()
            if existing:
                cursor.execute(adapt_sql(connection, """
                    UPDATE coverage_schema_meta
                    SET schema_version=?, applied_at=?, release_sha=?, migration_id=?
                    WHERE schema_key=?
                """), (version, _now(), release_sha or "", meta_migration, schema_key))
            else:
                cursor.execute(adapt_sql(connection, """
                    INSERT INTO coverage_schema_meta(
                        schema_key, schema_version, applied_at, release_sha, migration_id
                    ) VALUES (?, ?, ?, ?, ?)
                """), (schema_key, version, _now(), release_sha or "", meta_migration))
            cursor.close()
        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, """
            UPDATE coverage_schema_migrations
            SET state='APPLIED', finished_at=?, release_sha=?, error_class=''
            WHERE migration_id=?
        """), (_now(), release_sha or "", migration_id))
        cursor.close()
        connection.commit()
    except Exception as exc:
        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, """
            UPDATE coverage_schema_migrations
            SET state='FAILED', finished_at=?, release_sha=?, error_class=?
            WHERE migration_id=?
        """), (_now(), release_sha or "",
                type(exc).__name__, migration_id))
        cursor.close()
        connection.commit()
        raise
    return {"status": "PASSED", "migration_id": migration_id,
            "idempotent": False, "ddl_sha256": ddl_sha256}


def validate_migration_database_separation(source_config, target_config,
                                           source_connection=None,
                                           target_connection=None):
    """Reject a migration that could accidentally point both sides at one DB.

    Config equality is only a cheap preflight.  When connections are supplied
    the runtime fingerprint is authoritative, which prevents localhost/DNS
    aliases or different DB users from bypassing the same-instance check.
    """
    source = source_config.get("mysql") or source_config
    target = target_config.get("mysql") or target_config
    source_identity = (
        str(source.get("host", "127.0.0.1")), int(source.get("port", 3306)),
        str(source.get("user", "")), str(source.get("database", "")),
    )
    target_identity = (
        str(target.get("host", "127.0.0.1")), int(target.get("port", 3306)),
        str(target.get("user", "")), str(target.get("database", "")),
    )
    if source_identity == target_identity:
        raise ValueError("source and target database identities must be different")
    if not target_identity[3]:
        raise ValueError("target database name is required")
    result = {"source": source_identity, "target": target_identity,
              "configuration_check": "PASSED"}
    if (source_connection is None) != (target_connection is None):
        raise ValueError("source and target runtime connections must be supplied together")
    if source_connection is not None:
        runtime = assert_separate_connections(
            source_connection, target_connection,
            source_config=source, target_config=target,
        )
        result["runtime_fingerprint"] = runtime
    return result


def migrate_legacy(source_connection, target_connection, anomaly_path=None,
                   release_sha="", migration_id=None):
    """Migrate one legacy current-state snapshot idempotently."""
    source = capture_legacy_snapshot(source_connection)
    source_semantic = capture_legacy_semantic_snapshot(
        source_connection, snapshot=source
    )
    migration_id = migration_id or "legacy-v2-{}".format(
        semantic_hash(source_semantic)[:32]
    )
    anomalies = []
    project_repo = ProjectRepository()
    line_repo = LineIndexRepository()
    analysis_repo = AnalysisRepository()
    state_repo = ProjectStateRepository()
    file_state_repo = FileStateRepository()
    job_repo = JobRepository()

    with transaction(target_connection) as conn:
        for project_name in source["projects"]:
            project = project_repo.ensure_project(conn, project_name)
            data_version = int(source["project_data_versions"].get(project_name, 0))
            scan_key = hashlib.sha256(json.dumps({
                "project": project_name,
                "source": "legacy_migrated",
                "analysis_hash": semantic_hash([
                    row for row in source["analyses"] if row["project_name"] == project_name
                ]),
                "line_hash": semantic_hash([
                    row for row in source["lines"] if row["project_name"] == project_name
                ]),
            }, sort_keys=True).encode("utf-8")).hexdigest()
            existing_scan = project_repo.get_scan_by_key(conn, scan_key)
            scan = project_repo.create_scan(
                conn, project["id"], scan_key, "legacy_migrated", "full",
                status="building", legacy_migrated=1,
            )
            if existing_scan and str(existing_scan.get("status") or "").lower() not in {
                    "building", "importing", "constructing"}:
                # A completed migration is immutable.  Re-running the same
                # semantic snapshot must be a no-op for its physical facts.
                continue
            project_repo.upsert_repository_snapshot(
                conn, scan["id"], "", verified=0, provenance="legacy_migration"
            )
            project_repo.bind_report(
                conn, scan["id"], "legacy_{}".format(scan_key[:16]),
                source_signature="legacy_migration", sidecar_schema=0,
            )
            project_lines = [
                row for row in source["lines"] if row["project_name"] == project_name
            ]
            project_analyses = [
                row for row in source["analyses"] if row["project_name"] == project_name
            ]
            line_candidates = {}
            for row in project_lines:
                group_key = (row["file_path_hash"], row["line_number"])
                line_candidates.setdefault(group_key, set()).add(row.get("file_path") or "")
            for (file_hash, line_number), paths in sorted(line_candidates.items()):
                if len(paths) > 1:
                    anomalies.append({
                        "type": "path_conflict", "project_name": project_name,
                        "file_path_hash": file_hash, "line_number": line_number,
                        "paths": sorted(paths),
                    })
            conflict_hashes = {
                file_hash for (file_hash, _line_number), paths in line_candidates.items()
                if len(paths) > 1
            }
            # Build the immutable file and line identities in memory first.
            # The actual writes below are bounded batch operations.  A file is
            # still the natural validation boundary because a line can never
            # move between physical files after a scan is sealed.
            contexts = {}

            def repository_for(file_hash, path):
                if file_hash in conflict_hashes:
                    return "legacy-conflict-{}".format(
                        hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
                    )
                return ""

            def context_for(file_hash, path):
                repository_name = repository_for(file_hash, path)
                key = (repository_name, file_hash, path)
                if key not in contexts:
                    contexts[key] = {
                        "repository_name": repository_name,
                        "file_path_hash": file_hash,
                        "file_path": path,
                        "source_file_name": os.path.basename(path),
                        "lines": {},
                        "source_lines": {},
                        "analyses": {},
                    }
                return contexts[key]

            for source_line in project_lines:
                file_hash = source_line["file_path_hash"]
                line_number = source_line["line_number"]
                path = source_line.get("file_path") or ""
                if not path:
                    anomalies.append({
                        "type": "missing_file_path", "project_name": project_name,
                        "file_path_hash": file_hash, "line_number": line_number,
                    })
                    path = file_hash
                context = context_for(file_hash, path)
                context["source_lines"][line_number] = source_line
                line_record = dict(source_line)
                line_record["coverage_state"] = "uncovered"
                line_record["suggested_reviewer"] = ""
                context["lines"][line_number] = line_record

            for analysis_line in project_analyses:
                file_hash = analysis_line["file_path_hash"]
                line_number = analysis_line["line_number"]
                path = analysis_line.get("file_path") or ""
                if not path:
                    candidates = line_candidates.get((file_hash, line_number), set())
                    if len(candidates) == 1:
                        path = next(iter(candidates))
                    else:
                        anomalies.append({
                            "type": "analysis_path_ambiguous",
                            "project_name": project_name,
                            "file_path_hash": file_hash,
                            "line_number": line_number,
                            "paths": sorted(candidates),
                        })
                        path = file_hash
                context = context_for(file_hash, path)
                context["analyses"][line_number] = analysis_line
                if line_number not in context["lines"]:
                    anomalies.append({
                        "type": "missing_line_index_context", "project_name": project_name,
                        "file_path_hash": file_hash, "line_number": line_number,
                    })
                    context["lines"][line_number] = {
                        "line_number": line_number, "line_text": "",
                        "coverage_state": "uncovered",
                        "block_start_line": line_number, "block_end_line": line_number,
                        "block_type": "unknown", "function_name": "", "function_hash": "",
                        "code_line_hash": "", "code_occurrence": 1,
                        "suggested_reviewer": "",
                    }

            file_records = [
                {
                    "repository_name": item["repository_name"],
                    "file_path_hash": item["file_path_hash"],
                    "file_path": item["file_path"],
                    "source_file_name": item["source_file_name"],
                }
                for item in contexts.values()
            ]
            files = project_repo.ensure_files(conn, scan["id"], file_records)
            line_ids = {}
            provenance_rows = []
            for context_key in sorted(contexts):
                context = contexts[context_key]
                file_row = files[(context["repository_name"], context["file_path_hash"])]
                line_rows = line_repo.upsert_lines(
                    conn, file_row["id"],
                    [context["lines"][number] for number in sorted(context["lines"])]
                )
                by_number = {int(row["line_number"]): row for row in line_rows}
                for number, row in by_number.items():
                    line_ids[(context_key, number)] = row
                    source_line = context["source_lines"].get(number)
                    if source_line is not None:
                        provenance_rows.append({
                            "entity_type": "line", "target_entity_id": row["id"],
                            "source_table": "coverage_line_index",
                            "source_identity": "{}:{}:{}".format(
                                project_name, context["file_path_hash"], number
                            ),
                            "legacy_created_at": source_line.get("legacy_created_at"),
                            "legacy_updated_at": source_line.get("legacy_updated_at"),
                            "legacy_raw_status": None,
                            "legacy_raw_is_draft": None,
                            "raw_payload_sha256": source_line.get("raw_payload_sha256", ""),
                        })

            analysis_batch = []
            analysis_source_rows = []
            for context_key in sorted(contexts):
                context = contexts[context_key]
                for number in sorted(context["analyses"]):
                    source_analysis = context["analyses"][number]
                    line = line_ids[(context_key, number)]
                    analysis_batch.append(dict(source_analysis, line_id=line["id"]))
                    analysis_source_rows.append((line["id"], source_analysis))
            saved_analyses = analysis_repo.upsert_many(conn, analysis_batch)
            analyses_by_line = {int(row["line_id"]): row for row in saved_analyses}
            for line_id, source_analysis in analysis_source_rows:
                analysis = analyses_by_line.get(int(line_id))
                if not analysis:
                    raise RuntimeError("bulk analysis upsert did not return line identity")
                provenance_rows.append({
                    "entity_type": "legacy_analysis", "target_entity_id": analysis["id"],
                    "source_table": "coverage_analysis",
                    "source_identity": "{}:{}:{}".format(
                        project_name, source_analysis["file_path_hash"],
                        source_analysis["line_number"],
                    ),
                    "legacy_created_at": source_analysis.get("legacy_created_at"),
                    "legacy_updated_at": source_analysis.get("legacy_updated_at"),
                    "legacy_raw_status": source_analysis.get("status"),
                    "legacy_raw_is_draft": source_analysis.get("is_draft"),
                    "raw_payload_sha256": source_analysis.get("raw_payload_sha256", ""),
                })
            state_repo.ensure(
                conn, project["id"], current_scan_id=scan["id"], data_version=data_version
            )
            project_metadata = source.get("project_metadata", {}).get(project_name)
            if project_metadata is not None:
                provenance_rows.append({
                    "entity_type": "project_state", "target_entity_id": project["id"],
                    "source_table": "coverage_project_state",
                    "source_identity": project_name,
                    "legacy_created_at": None,
                    "legacy_updated_at": project_metadata.get("updated_at"),
                    "legacy_raw_status": None,
                    "legacy_raw_is_draft": None,
                    "raw_payload_sha256": project_metadata.get("raw_payload_sha256", ""),
                })
            _upsert_legacy_provenance_many(conn, migration_id, provenance_rows)
            state_repo.set_current_scan(conn, project["id"], scan["id"])
            file_state_repo.rebuild_scan(conn, scan["id"], data_version, None)
            state_repo.mark_ready(conn, project["id"], data_version)
            project_repo.seal_scan(conn, scan["id"])

        project_by_name = {
            row["project_name"]: row for row in project_repo.list_projects(conn)
        }
        for old_job in source["jobs"]:
            project = project_by_name.get(old_job["project_name"])
            if not project:
                anomalies.append({"type": "orphan_job", "job_id": old_job["job_id"]})
                continue
            state = old_job["state"]
            if state in ("queued", "running", "interrupted"):
                anomalies.append({
                    "type": "active_job_requires_decision",
                    "job_id": old_job["job_id"], "legacy_state": state,
                })
                state = "interrupted"
            job_repo.upsert(conn, {
                "job_id": old_job["job_id"], "project_id": project["id"],
                "kind": old_job["kind"], "state": state,
                "progress": 0, "input_payload": json.dumps(
                    old_job, sort_keys=True, default=_semantic_json_default,
                ),
                "error_message": old_job["error_message"] if state != "interrupted"
                else "legacy active job requires manual migration decision",
                "data_version": old_job["data_version"],
                "handler_version": "legacy-migration-v2",
                "legacy_raw_percent": old_job.get("legacy_raw_percent"),
                "legacy_percent_unit": old_job.get("legacy_percent_unit", ""),
            })
            # A legacy result path is provenance only.  Do not make an
            # untrusted historical path an active downloadable target.
            cursor = conn.cursor()
            cursor.execute(adapt_sql(conn, """
                UPDATE coverage_background_jobs
                SET handler_version=?, legacy_raw_percent=?, legacy_percent_unit=?
                WHERE job_id=?
            """), ("legacy-migration-v2", old_job.get("legacy_raw_percent"),
                    old_job.get("legacy_percent_unit", ""), old_job["job_id"]))
            cursor.close()
            job_target_id = int(hashlib.sha256(
                str(old_job["job_id"]).encode("utf-8")
            ).hexdigest()[:15], 16)
            _upsert_legacy_provenance_many(conn, migration_id, [{
                "entity_type": "job", "target_entity_id": job_target_id,
                "source_table": "coverage_background_jobs",
                "source_identity": old_job["job_id"],
                "legacy_created_at": old_job.get("created_at"),
                "legacy_updated_at": old_job.get("updated_at"),
                "legacy_raw_status": old_job.get("state"),
                "legacy_raw_is_draft": None,
                "raw_payload_sha256": old_job.get("raw_payload_sha256", ""),
                "raw_payload": json.dumps(
                    old_job, ensure_ascii=False, sort_keys=True,
                    default=_semantic_json_default,
                ),
            }])

    result = {
        "status": "PASSED",
        "source_projects": len(source["projects"]),
        "source_line_facts": len(source["lines"]),
        "source_analysis_facts": len(source["analyses"]),
        "source_jobs": len(source["jobs"]),
        "anomalies": anomalies,
        "source_semantic_hash": semantic_hash(source_semantic),
        "target_semantic_hash": semantic_hash(
            capture_vnext_semantic_snapshot(target_connection)
        ),
        "authoritative_semantic_match": semantic_hash(source_semantic) == semantic_hash(
            capture_vnext_semantic_snapshot(target_connection)
        ),
        "release_sha": release_sha or "",
        "migration_id": migration_id,
        "captured_at": utc_iso(),
    }
    if anomaly_path:
        parent = os.path.dirname(os.path.abspath(anomaly_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(anomaly_path, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
    return result


def _demo_connections():
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(":memory:")
    target.row_factory = sqlite3.Row
    create_sqlite_schema(target)
    return source, target


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Migrate a read-only legacy DB into VNext")
    parser.add_argument("--demo", action="store_true",
                        help="run a self-contained SQLite smoke migration")
    parser.add_argument("--anomaly-path", default="")
    parser.add_argument("--source-config", default="")
    parser.add_argument("--target-config", default="")
    parser.add_argument("--schema", default="")
    parser.add_argument("--release-sha", default="")
    args = parser.parse_args(argv)
    if args.demo:
        source, target = _demo_connections()
        result = migrate_legacy(source, target, args.anomaly_path or None)
        print(json.dumps(
            result, ensure_ascii=False, indent=2, sort_keys=True,
            default=_semantic_json_default,
        ))
        return 0
    if args.source_config and args.target_config:
        import pymysql

        def load_mysql_config(path):
            with open(path, "r", encoding="utf-8") as stream:
                config = json.load(stream)
            return config.get("mysql") or config

        def connect(config):
            return pymysql.connect(
                host=config.get("host", "127.0.0.1"),
                port=int(config.get("port", 3306)),
                user=config.get("user", "root"),
                password=str(config.get("password", "")),
                database=config.get("database"),
                charset="utf8mb4",
                autocommit=False,
                connect_timeout=float(config.get("connect_timeout", 5)),
                cursorclass=pymysql.cursors.DictCursor,
            )

        source_config = load_mysql_config(args.source_config)
        target_config = load_mysql_config(args.target_config)
        validate_migration_database_separation(source_config, target_config)
        source = connect(source_config)
        target = connect(target_config)
        try:
            if not args.schema:
                parser.error("--schema is required for MySQL migration")
            apply_schema(target, args.schema, args.release_sha)
            first = migrate_legacy(source, target, args.anomaly_path or None, args.release_sha)
            # The source connection is never used for writes.  Rollback also
            # releases any driver-side read snapshot before the second audit.
            source.rollback()
            first_snapshot = capture_vnext_semantic_snapshot(target)
            second = migrate_legacy(source, target, args.anomaly_path or None, args.release_sha)
            second_snapshot = capture_vnext_semantic_snapshot(target)
            result = {
                "status": "PASSED" if (
                    first.get("authoritative_semantic_match")
                    and second.get("authoritative_semantic_match")
                    and first_snapshot == second_snapshot
                ) else "FAILED",
                "first_run": first,
                "second_run": second,
                "idempotent": first_snapshot == second_snapshot,
                "source_revision": args.release_sha or "",
            }
            if args.anomaly_path:
                with open(args.anomaly_path, "w", encoding="utf-8") as stream:
                    json.dump(
                        result, stream, ensure_ascii=False, indent=2, sort_keys=True,
                        default=_semantic_json_default,
                    )
            print(json.dumps(
                result, ensure_ascii=False, indent=2, sort_keys=True,
                default=_semantic_json_default,
            ))
            return 0 if result["status"] == "PASSED" else 1
        finally:
            source.close()
            target.close()
    parser.error("provide --demo or explicit --source-config, --target-config and --schema")


if __name__ == "__main__":
    sys.exit(main())
