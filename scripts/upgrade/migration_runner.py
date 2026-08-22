"""Legacy-to-VNext migration runner and semantic integrity snapshots."""

from __future__ import print_function

import hashlib
import json
import os
import pickle
import sqlite3
import sys
import tempfile
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
from app.db.repositories.base import (
    adapt_sql, bind_chunk_size, fetchall, fetchone, insert_id, is_sqlite,
    iter_rows,
)
from app.db.identity_keys import stable_identity_hash
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
    handler_version TEXT NOT NULL DEFAULT '',
    heartbeat_at TEXT, lease_owner TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
    started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS coverage_incremental_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scan_id INTEGER NOT NULL,
    report_id TEXT NOT NULL DEFAULT '', repository_name TEXT NOT NULL,
    incremental_key_hash TEXT NOT NULL,
    old_commit_sha TEXT NOT NULL DEFAULT '', new_commit_sha TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL, generated_at TEXT NOT NULL, UNIQUE(incremental_key_hash)
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
    error_class TEXT NOT NULL DEFAULT '',
    target_database TEXT NOT NULL DEFAULT '',
    target_runtime_fingerprint TEXT NOT NULL DEFAULT '',
    target_table_inventory_hash TEXT NOT NULL DEFAULT '',
    target_emptiness_result TEXT NOT NULL DEFAULT '',
    target_preflight_at TEXT
);
CREATE TABLE IF NOT EXISTS coverage_legacy_provenance (
    id INTEGER PRIMARY KEY AUTOINCREMENT, migration_id TEXT NOT NULL,
    target_entity_type TEXT NOT NULL, target_entity_id INTEGER NOT NULL,
    source_table TEXT NOT NULL, source_identity TEXT NOT NULL,
    provenance_key_hash TEXT NOT NULL,
    legacy_created_at TEXT, legacy_updated_at TEXT,
    legacy_raw_status TEXT, legacy_raw_is_draft INTEGER,
    raw_payload_sha256 TEXT NOT NULL, raw_payload TEXT, created_at TEXT NOT NULL,
    UNIQUE(provenance_key_hash)
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
    failure_key_hash TEXT NOT NULL,
    message_redacted TEXT, fencing_token INTEGER, occurred_at TEXT NOT NULL,
    UNIQUE(failure_key_hash)
);
CREATE TABLE IF NOT EXISTS coverage_migration_checkpoints (
    migration_id TEXT NOT NULL, checkpoint_key TEXT NOT NULL,
    checkpoint_key_hash TEXT NOT NULL,
    phase TEXT NOT NULL, source_cursor TEXT NOT NULL DEFAULT '',
    semantic_fragment_hash TEXT NOT NULL DEFAULT '', target_counts TEXT NOT NULL DEFAULT '',
    migration_version INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY (checkpoint_key_hash)
);
"""


SQLITE_ADDITIVE_COLUMNS = {
    "coverage_schema_meta": [
        ("migration_id", "TEXT NOT NULL DEFAULT ''"),
    ],
    "coverage_schema_migrations": [
        ("target_database", "TEXT NOT NULL DEFAULT ''"),
        ("target_runtime_fingerprint", "TEXT NOT NULL DEFAULT ''"),
        ("target_table_inventory_hash", "TEXT NOT NULL DEFAULT ''"),
        ("target_emptiness_result", "TEXT NOT NULL DEFAULT ''"),
        ("target_preflight_at", "TEXT"),
    ],
    "coverage_incremental_results": [
        ("incremental_key_hash", "TEXT NOT NULL DEFAULT ''"),
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


def _migration_checkpoint_key_hash(migration_id, checkpoint_key):
    """Return the bounded, backend-neutral identity for one checkpoint.

    The human-readable key is deliberately retained for diagnostics, but it
    can contain a Unicode path and therefore cannot safely participate in a
    MariaDB 5.5 utf8mb4 primary key. Hashing the pair preserves exact
    identity without relying on a lossy prefix index.
    """
    payload = "{}\x00{}".format(migration_id, checkpoint_key).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _ensure_migration_checkpoint_table(connection):
    """Install the additive migration checkpoint table on older VNext DBs.

    The table is migration metadata, not business data. It is nevertheless
    required by the streaming runner, so an already-created VNext schema must
    receive it before a migration is resumed. Keep the DDL compatible with
    both SQLite and MariaDB 5.5 without relying on ``ADD COLUMN IF NOT
    EXISTS``.
    """
    ddl = """
        CREATE TABLE IF NOT EXISTS coverage_migration_checkpoints (
            migration_id {migration_id} NOT NULL,
            checkpoint_key {checkpoint_key} NOT NULL,
            checkpoint_key_hash {checkpoint_key_hash} NOT NULL,
            phase {phase} NOT NULL,
            source_cursor {source_cursor} NOT NULL DEFAULT '',
            semantic_fragment_hash {fragment_hash} NOT NULL DEFAULT '',
            target_counts {target_counts} NOT NULL,
            migration_version INTEGER NOT NULL DEFAULT 1,
            state {state} NOT NULL,
            updated_at {updated_at} NOT NULL,
            PRIMARY KEY (checkpoint_key_hash)
        )
    """
    if is_sqlite(connection):
        statement = ddl.format(
            migration_id="TEXT", checkpoint_key="TEXT", phase="TEXT",
            checkpoint_key_hash="TEXT",
            source_cursor="TEXT", fragment_hash="TEXT", target_counts="TEXT",
            state="TEXT", updated_at="TEXT",
        )
    else:
        statement = ddl.format(
            migration_id="VARCHAR(128)", checkpoint_key="TEXT",
            checkpoint_key_hash=(
                "CHAR(64) CHARACTER SET ascii COLLATE ascii_bin"
            ),
            phase="VARCHAR(64)", source_cursor="VARCHAR(512)",
            fragment_hash="CHAR(64)", target_counts="LONGTEXT",
            state="VARCHAR(32)", updated_at="DATETIME",
        )
    if not _table_exists(connection, "coverage_migration_checkpoints"):
        cursor = connection.cursor()
        try:
            cursor.execute(adapt_sql(connection, statement))
        finally:
            cursor.close()
        return

    # Older VNext targets used a composite utf8mb4 primary key. Upgrade that
    # metadata table in place so a MariaDB 5.5 retry has the same bounded key
    # contract as a fresh target. The readable columns remain unchanged.
    if not _column_exists(
            connection, "coverage_migration_checkpoints", "checkpoint_key_hash"):
        definition = (
            "TEXT NOT NULL DEFAULT ''" if is_sqlite(connection) else
            "CHAR(64) CHARACTER SET ascii COLLATE ascii_bin "
            "NOT NULL DEFAULT ''"
        )
        cursor = connection.cursor()
        try:
            cursor.execute(adapt_sql(connection, "ALTER TABLE "
                                      "coverage_migration_checkpoints "
                                      "ADD COLUMN checkpoint_key_hash " + definition))
        finally:
            cursor.close()

    rows = fetchall(connection, """
        SELECT migration_id, checkpoint_key, checkpoint_key_hash
        FROM coverage_migration_checkpoints
    """)
    cursor = connection.cursor()
    try:
        for row in rows:
            migration_id = row.get("migration_id")
            checkpoint_key = row.get("checkpoint_key")
            expected = _migration_checkpoint_key_hash(migration_id, checkpoint_key)
            if str(row.get("checkpoint_key_hash") or "") == expected:
                continue
            cursor.execute(adapt_sql(connection, """
                UPDATE coverage_migration_checkpoints
                SET checkpoint_key_hash=?
                WHERE migration_id=? AND checkpoint_key=?
            """), (expected, migration_id, checkpoint_key))
    finally:
        cursor.close()

    if is_sqlite(connection):
        return

    primary_rows = fetchall(connection, """
        SELECT COLUMN_NAME
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA=DATABASE()
          AND TABLE_NAME=?
          AND INDEX_NAME='PRIMARY'
        ORDER BY SEQ_IN_INDEX
    """, ("coverage_migration_checkpoints",))
    primary_columns = [str(row.get("COLUMN_NAME") or "") for row in primary_rows]
    needs_new_primary = primary_columns != ["checkpoint_key_hash"]
    if needs_new_primary and primary_columns:
        cursor = connection.cursor()
        try:
            cursor.execute(adapt_sql(
                connection,
                "ALTER TABLE coverage_migration_checkpoints DROP PRIMARY KEY",
            ))
        finally:
            cursor.close()

    if not is_sqlite(connection):
        key_type = fetchone(connection, """
            SELECT DATA_TYPE
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA=DATABASE()
              AND TABLE_NAME=?
              AND COLUMN_NAME='checkpoint_key'
        """, ("coverage_migration_checkpoints",))
        if str((key_type or {}).get("DATA_TYPE") or "").lower() != "text":
            cursor = connection.cursor()
            try:
                cursor.execute(adapt_sql(
                    connection,
                    "ALTER TABLE coverage_migration_checkpoints "
                    "MODIFY checkpoint_key TEXT NOT NULL",
                ))
            finally:
                cursor.close()

    if not is_sqlite(connection) and needs_new_primary:
        cursor = connection.cursor()
        try:
            cursor.execute(adapt_sql(
                connection,
                "ALTER TABLE coverage_migration_checkpoints "
                "ADD PRIMARY KEY (checkpoint_key_hash)",
            ))
        finally:
            cursor.close()


def _column_exists(connection, table_name, column_name):
    if is_sqlite(connection):
        rows = connection.execute("PRAGMA table_info({})".format(table_name)).fetchall()
        return any(str(row[1]) == column_name for row in rows)
    row = fetchone(connection, """
        SELECT COLUMN_NAME FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND COLUMN_NAME = ?
    """, (table_name, column_name))
    return bool(row)


def _index_exists(connection, table_name, index_name):
    if is_sqlite(connection):
        rows = connection.execute(
            "PRAGMA index_list({})".format(table_name)
        ).fetchall()
        return any(str(row[1]) == index_name for row in rows)
    row = fetchone(connection, """
        SELECT INDEX_NAME FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND INDEX_NAME = ?
    """, (table_name, index_name))
    return bool(row)


RUNTIME_INDEXES = (
    ("coverage_background_jobs", "idx_vnext_jobs_active_identity",
     "project_id, scan_id, kind, data_version, state, created_at, job_id"),
    ("coverage_background_jobs", "idx_vnext_jobs_recovery",
     "state, heartbeat_at, created_at, job_id"),
    ("coverage_background_jobs", "idx_vnext_jobs_created_cursor",
     "created_at, job_id"),
    ("coverage_file_state", "idx_vnext_file_state_pending",
     "scan_id, pending_total, file_id"),
    ("coverage_analysis_line_links", "idx_vnext_links_scan_active_line",
     "scan_id, is_active, line_id"),
)


def _ensure_runtime_indexes(connection):
    """Install bounded hot-path indexes using MariaDB-5.5-safe additive DDL."""
    for table_name, index_name, columns in RUNTIME_INDEXES:
        if not _table_exists(connection, table_name) or _index_exists(
                connection, table_name, index_name):
            continue
        cursor = connection.cursor()
        try:
            if is_sqlite(connection):
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS {} ON {} ({})".format(
                        index_name, table_name, columns
                    )
                )
            else:
                cursor.execute(adapt_sql(connection, """
                    ALTER TABLE {table} ADD INDEX {index} ({columns})
                """.format(table=table_name, index=index_name, columns=columns)))
        finally:
            cursor.close()


def _legacy_provenance_key_hash(migration_id, entity_type,
                                target_entity_id, source_table):
    """Return a bounded identity key that remains indexable on MariaDB 5.5."""
    payload = json.dumps([
        str(migration_id or ""), str(entity_type or ""),
        int(target_entity_id or 0), str(source_table or ""),
    ], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ensure_legacy_provenance_key_hash(connection):
    """Backfill the bounded key and install its MariaDB-safe unique index."""
    table_name = "coverage_legacy_provenance"
    if not _table_exists(connection, table_name) or not _column_exists(
            connection, table_name, "provenance_key_hash"):
        return
    rows = fetchall(connection, """
        SELECT id, migration_id, target_entity_type, target_entity_id,
               source_table, provenance_key_hash
        FROM coverage_legacy_provenance
    """)
    updates = []
    for row in rows:
        expected = _legacy_provenance_key_hash(
            row.get("migration_id"), row.get("target_entity_type"),
            row.get("target_entity_id"), row.get("source_table"),
        )
        if str(row.get("provenance_key_hash") or "") != expected:
            updates.append((expected, int(row.get("id") or 0)))
    if updates:
        cursor = connection.cursor()
        try:
            cursor.executemany(adapt_sql(connection, """
                UPDATE coverage_legacy_provenance
                SET provenance_key_hash=? WHERE id=?
            """), updates)
        finally:
            cursor.close()
    index_name = "uq_legacy_provenance_hash"
    if _index_exists(connection, table_name, index_name):
        return
    cursor = connection.cursor()
    try:
        if is_sqlite(connection):
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} (provenance_key_hash)".format(
                    index_name, table_name
                )
            )
        else:
            cursor.execute(adapt_sql(connection, """
                ALTER TABLE coverage_legacy_provenance
                ADD UNIQUE KEY uq_legacy_provenance_hash (provenance_key_hash)
            """))
    finally:
        cursor.close()


def _ensure_incremental_result_key_hash(connection):
    table_name = "coverage_incremental_results"
    column_name = "incremental_key_hash"
    if not _table_exists(connection, table_name) or not _column_exists(
            connection, table_name, column_name):
        return
    rows = fetchall(connection, """
        SELECT id, scan_id, report_id, repository_name, incremental_key_hash
        FROM coverage_incremental_results
    """)
    updates = []
    for row in rows:
        expected = stable_identity_hash(
            int(row.get("scan_id") or 0), row.get("report_id") or "",
            row.get("repository_name") or "",
        )
        if str(row.get(column_name) or "") != expected:
            updates.append((expected, int(row.get("id") or 0)))
    if updates:
        cursor = connection.cursor()
        try:
            cursor.executemany(adapt_sql(connection, """
                UPDATE coverage_incremental_results
                SET incremental_key_hash=? WHERE id=?
            """), updates)
        finally:
            cursor.close()
    index_name = "uq_incremental_key_hash"
    if _index_exists(connection, table_name, index_name):
        return
    cursor = connection.cursor()
    try:
        if is_sqlite(connection):
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} ({})".format(
                    index_name, table_name, column_name
                )
            )
        else:
            cursor.execute(adapt_sql(connection, """
                ALTER TABLE coverage_incremental_results
                ADD UNIQUE KEY uq_incremental_key_hash (incremental_key_hash)
            """))
    finally:
        cursor.close()


def _ensure_import_failure_key_hash(connection):
    table_name = "coverage_import_failures"
    column_name = "failure_key_hash"
    if not _table_exists(connection, table_name) or not _column_exists(
            connection, table_name, column_name):
        return
    rows = fetchall(connection, """
        SELECT id, job_id, phase, error_fingerprint, failure_key_hash
        FROM coverage_import_failures
    """)
    updates = []
    for row in rows:
        expected = stable_identity_hash(
            row.get("job_id") or "", row.get("phase") or "",
            row.get("error_fingerprint") or "",
        )
        if str(row.get(column_name) or "") != expected:
            updates.append((expected, int(row.get("id") or 0)))
    if updates:
        cursor = connection.cursor()
        try:
            cursor.executemany(adapt_sql(connection, """
                UPDATE coverage_import_failures
                SET failure_key_hash=? WHERE id=?
            """), updates)
        finally:
            cursor.close()
    index_name = "uq_import_failure_hash"
    if _index_exists(connection, table_name, index_name):
        return
    cursor = connection.cursor()
    try:
        if is_sqlite(connection):
            cursor.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} ({})".format(
                    index_name, table_name, column_name
                )
            )
        else:
            cursor.execute(adapt_sql(connection, """
                ALTER TABLE coverage_import_failures
                ADD UNIQUE KEY uq_import_failure_hash (failure_key_hash)
            """))
    finally:
        cursor.close()


def _rows(connection, table_name):
    if not _table_exists(connection, table_name):
        return []
    return fetchall(connection, "SELECT * FROM {}".format(table_name))


def _project_names(connection):
    names = set()
    for table in ("coverage_analysis", "coverage_line_index",
                  "coverage_project_state", "coverage_background_jobs"):
        if not _table_exists(connection, table):
            continue
        for row in fetchall(connection, """
            SELECT DISTINCT project_name FROM {} WHERE project_name IS NOT NULL
        """.format(table)):
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


LEGACY_STREAM_BATCH_SIZE = 500


def _iter_legacy_rows(connection, table_name, where="", params=(),
                      batch_size=LEGACY_STREAM_BATCH_SIZE):
    """Yield legacy rows through an immutable primary-key cursor.

    The source schema is historical but all business tables in the supported
    v10 inventory have a primary key (``id`` for facts, ``job_id`` for jobs).
    Fact migration uses this helper instead of a cursor that remains open
    across target transactions, so the source connection can stay read-only.
    """
    if not _table_exists(connection, table_name):
        return
    if table_name == "coverage_background_jobs":
        cursor_value = ""
        key_column = "job_id"
        order = "job_id"
        predicate = "job_id > ?"
    elif table_name == "coverage_project_state":
        cursor_value = ""
        key_column = "project_name"
        order = "project_name"
        predicate = "project_name > ?"
    else:
        cursor_value = 0
        key_column = "id"
        order = "id"
        predicate = "id > ?"
    while True:
        clauses = [predicate]
        values = [cursor_value]
        if where:
            clauses.insert(0, "(" + where + ")")
            values = list(params) + values
        rows = fetchall(connection, """
            SELECT * FROM {table}
            WHERE {where}
            ORDER BY {order} LIMIT ?
        """.format(table=table_name, where=" AND ".join(clauses), order=order),
            values + [int(batch_size)])
        if not rows:
            return
        for row in rows:
            yield row
        next_cursor = rows[-1].get(key_column)
        if next_cursor in (None, cursor_value):
            raise RuntimeError("legacy keyset cursor did not advance")
        cursor_value = next_cursor
        if len(rows) < int(batch_size):
            return


def _legacy_analysis_fact(row):
    file_hash, line_number, path = _legacy_key(row)
    return {
        "source_pk": _nullable_int(row.get("id")),
        "project_name": str(row.get("project_name") or ""),
        "file_path_hash": file_hash, "file_path": path,
        "line_number": line_number, "status": str(row.get("status") or ""),
        "is_draft": int(row.get("is_draft") or 0),
        "reviewer": str(row.get("reviewer") or ""),
        "coverage_method": _legacy_text(row, ("coverage_method", "method")),
        "uncovered_reason": _legacy_text(row, ("uncovered_reason", "reason")),
        "comment": _legacy_text(row, ("comment", "comments", "remark")),
        "legacy_created_at": row.get("created_at"),
        "legacy_updated_at": row.get("updated_at"),
        "raw_payload_sha256": _legacy_payload_hash(row),
    }


def _legacy_line_fact(row):
    file_hash, line_number, path = _legacy_key(row)
    return {
        "source_pk": _nullable_int(row.get("id")),
        "project_name": str(row.get("project_name") or ""),
        "file_path_hash": file_hash, "file_path": path,
        "line_number": line_number, "line_text": str(row.get("line_text") or ""),
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
    }


def _legacy_job_fact(row):
    return {
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
    }


def _legacy_project_data_versions(connection, projects):
    result = {}
    if not _table_exists(connection, "coverage_project_state"):
        return result
    for project_name in projects:
        row = fetchone(connection, """
            SELECT data_version FROM coverage_project_state WHERE project_name=?
        """, (project_name,))
        result[project_name] = int((row or {}).get("data_version") or 0)
    return result


def _legacy_file_hash_page(connection, project_name, cursor="", limit=500):
    sources = []
    if _table_exists(connection, "coverage_line_index"):
        sources.append(
            "SELECT file_path_hash FROM coverage_line_index WHERE project_name=?"
        )
    if _table_exists(connection, "coverage_analysis"):
        sources.append(
            "SELECT file_path_hash FROM coverage_analysis WHERE project_name=?"
        )
    if not sources:
        return []
    query = " UNION ".join(sources)
    rows = fetchall(connection, """
        SELECT file_path_hash FROM ({}) AS legacy_file_keys
        WHERE file_path_hash > ? ORDER BY file_path_hash LIMIT ?
    """.format(query), [project_name] * len(sources) + [cursor, int(limit)])
    return [str(row.get("file_path_hash") or "") for row in rows]


def _legacy_file_contexts(connection, project_name, file_hash):
    """Materialize one physical file/hash group, never the whole project."""
    lines = [
        _legacy_line_fact(row) for row in _iter_legacy_rows(
            connection, "coverage_line_index",
            "project_name=? AND file_path_hash=?", (project_name, file_hash)
        )
    ]
    analyses = [
        _legacy_analysis_fact(row) for row in _iter_legacy_rows(
            connection, "coverage_analysis",
            "project_name=? AND file_path_hash=?", (project_name, file_hash)
        )
    ]
    paths_by_line = {}
    for line in lines:
        path = line.get("file_path") or ""
        if path:
            paths_by_line.setdefault(int(line["line_number"]), set()).add(path)
    conflict = any(len(paths) > 1 for paths in paths_by_line.values())
    contexts = {}

    def context_for(path):
        path = str(path or file_hash)
        key = ("legacy-conflict-" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
               if conflict else "", file_hash, path)
        if key not in contexts:
            contexts[key] = {
                "repository_name": key[0], "file_path_hash": file_hash,
                "file_path": path, "source_file_name": os.path.basename(path),
                "lines": {}, "source_lines": {}, "analyses": {},
                "path_conflict": conflict,
                "path_candidates": {
                    number: sorted(values) for number, values in paths_by_line.items()
                },
                "missing_file_path_lines": [], "ambiguous_analysis_lines": [],
                "missing_line_index_lines": [],
            }
        return contexts[key]

    for line in lines:
        path = line.get("file_path") or ""
        if not path:
            context["missing_file_path_lines"].append(int(line["line_number"]))
            path = file_hash
        context = context_for(path)
        number = int(line["line_number"])
        context["source_lines"][number] = line
        context["lines"][number] = dict(
            line, coverage_state="uncovered", suggested_reviewer=""
        )
    for analysis in analyses:
        number = int(analysis["line_number"])
        path = analysis.get("file_path") or ""
        candidates = paths_by_line.get(number, set())
        if not path:
            if len(candidates) != 1:
                # Keep the explicit ambiguity evidence with the bounded file
                # context; the write path still uses the deterministic hash
                # namespace fallback below.
                context = context_for(file_hash)
                context["ambiguous_analysis_lines"].append(number)
            path = next(iter(candidates)) if len(candidates) == 1 else file_hash
        context = context_for(path)
        context["analyses"][number] = analysis
        if number not in context["lines"]:
            context["missing_line_index_lines"].append(number)
            context["lines"][number] = {
                "line_number": number, "line_text": "", "coverage_state": "uncovered",
                "block_start_line": number, "block_end_line": number,
                "block_type": "unknown", "function_name": "", "function_hash": "",
                "code_line_hash": "", "code_occurrence": 1, "suggested_reviewer": "",
            }
    return [contexts[key] for key in sorted(contexts)]


def _iter_legacy_file_contexts(connection, project_name, batch_size=500):
    cursor = ""
    while True:
        hashes = _legacy_file_hash_page(connection, project_name, cursor, batch_size)
        if not hashes:
            return
        for file_hash in hashes:
            for context in _legacy_file_contexts(connection, project_name, file_hash):
                yield context
        next_cursor = hashes[-1]
        if next_cursor == cursor:
            raise RuntimeError("legacy file hash cursor did not advance")
        cursor = next_cursor
        if len(hashes) < int(batch_size):
            return


def _json_compact(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=_semantic_json_default,
    ).encode("utf-8")


def _stream_json_array(hasher, items):
    hasher.update(b"[")
    first = True
    for item in items:
        if not first:
            hasher.update(b",")
        hasher.update(_json_compact(item))
        first = False
    hasher.update(b"]")


def _semantic_line_item(line):
    return {key: line.get(key) for key in (
        "project_name", "file_path_hash", "file_path", "line_number", "line_text",
        "block_start_line", "block_end_line", "block_type", "function_name",
        "function_hash", "code_line_hash", "code_occurrence",
    )}


def _semantic_analysis_item(row):
    return {key: row.get(key) for key in (
        "project_name", "file_path_hash", "file_path", "line_number", "status",
        "is_draft", "reviewer", "coverage_method", "uncovered_reason", "comment",
    )}


def _source_provenance_item(entity_type, source_table, identity, row,
                            raw_status=None, raw_is_draft=None):
    return {
        "target_entity_type": entity_type, "source_table": source_table,
        "source_identity": identity,
        "legacy_created_at": row.get("legacy_created_at", row.get("created_at")),
        "legacy_updated_at": row.get("legacy_updated_at", row.get("updated_at")),
        "legacy_raw_status": raw_status, "legacy_raw_is_draft": raw_is_draft,
        "raw_payload_sha256": row.get("raw_payload_sha256") or "",
    }


class _JsonArraySpool(object):
    """Disk-backed JSON-array body used to keep migration hashing bounded."""

    def __init__(self):
        self.stream = tempfile.TemporaryFile(mode="w+b")
        self.has_items = False

    def append(self, item):
        self.append_encoded(_json_compact(item))

    def append_encoded(self, encoded):
        if self.has_items:
            self.stream.write(b",")
        self.stream.write(encoded)
        self.has_items = True

    def copy_to(self, target):
        self.stream.seek(0)
        while True:
            chunk = self.stream.read(1024 * 1024)
            if not chunk:
                return
            target.update(chunk)

    def close(self):
        self.stream.close()


class _LegacySourceSpool(object):
    """Replayable disk-backed source facts for the migration write pass.

    The source semantic hash and the target write path need the same normalized
    legacy contexts.  Keeping their byte offsets instead of the contexts in a
    Python list makes the source DB a single-pass input while keeping replay
    memory bounded by one physical file context.
    """

    def __init__(self):
        self.stream = tempfile.TemporaryFile(mode="w+b")
        self.context_offsets = {}
        self.project_state_offsets = {}
        self.job_offsets = []

    def _append(self, record):
        offset = self.stream.tell()
        pickle.dump(record, self.stream, protocol=2)
        return offset

    def append_context(self, project_name, context):
        offset = self._append(("context", project_name, context))
        self.context_offsets.setdefault(project_name, []).append(offset)

    def append_project_state(self, project_name, state):
        self.project_state_offsets[project_name] = self._append(
            ("project_state", project_name, state)
        )

    def append_job(self, fact):
        self.job_offsets.append(self._append(("job", fact)))

    def _read_at(self, offset):
        self.stream.seek(offset)
        return pickle.load(self.stream)

    def iter_contexts(self, project_name):
        self.stream.flush()
        for offset in self.context_offsets.get(project_name, ()):
            record = self._read_at(offset)
            if record[0] != "context" or record[1] != project_name:
                raise RuntimeError("legacy source spool context index mismatch")
            yield record[2]

    def project_state(self, project_name):
        self.stream.flush()
        offset = self.project_state_offsets.get(project_name)
        if offset is None:
            return {}
        record = self._read_at(offset)
        if record[0] != "project_state" or record[1] != project_name:
            raise RuntimeError("legacy source spool project state index mismatch")
        return record[2]

    def iter_jobs(self):
        self.stream.flush()
        for offset in self.job_offsets:
            record = self._read_at(offset)
            if record[0] != "job":
                raise RuntimeError("legacy source spool job index mismatch")
            yield record[1]

    def close(self):
        self.stream.close()


def _stream_legacy_semantic_hash(connection, projects, data_versions,
                                 source_spool=None):
    """Hash normalized legacy facts with one bounded source traversal.

    The semantic contract keeps its historical field order, even though the
    source facts are naturally encountered file-by-file.  Disk-backed spools
    let one traversal feed the analyses, lines, and provenance fields without
    retaining the complete source snapshot or querying each file context again.
    """
    project_names = sorted(projects)
    counts = {"lines": 0, "analyses": 0, "jobs": 0}
    array_spools = {
        "analyses": _JsonArraySpool(),
        "jobs": _JsonArraySpool(),
        "lines": _JsonArraySpool(),
        "projects": _JsonArraySpool(),
    }
    provenance_spools = {
        "coverage_analysis": _JsonArraySpool(),
        "coverage_background_jobs": _JsonArraySpool(),
        "coverage_line_index": _JsonArraySpool(),
        "coverage_project_state": _JsonArraySpool(),
    }
    line_fragment_spool = tempfile.TemporaryFile(mode="w+b")
    analysis_fragment_spool = tempfile.TemporaryFile(mode="w+b")
    fragment_offsets = {}

    try:
        for project_name in project_names:
            line_start = line_fragment_spool.tell()
            analysis_start = analysis_fragment_spool.tell()
            for context in _iter_legacy_file_contexts(connection, project_name):
                if source_spool is not None:
                    source_spool.append_context(project_name, context)
                for number in sorted(context["analyses"]):
                    row = context["analyses"][number]
                    item = _semantic_analysis_item(dict(
                        row, project_name=project_name,
                        file_path_hash=context["file_path_hash"],
                        file_path=context["file_path"],
                    ))
                    encoded = _json_compact(item)
                    array_spools["analyses"].append_encoded(encoded)
                    analysis_fragment_spool.write(b"A" + encoded)
                    counts["analyses"] += 1

                for number in sorted(context["lines"]):
                    row = context["lines"][number]
                    item = _semantic_line_item(dict(
                        row, project_name=project_name,
                        file_path_hash=context["file_path_hash"],
                        file_path=context["file_path"],
                    ))
                    encoded = _json_compact(item)
                    array_spools["lines"].append_encoded(encoded)
                    line_fragment_spool.write(b"L" + encoded)
                    source_line = context.get("source_lines", {}).get(number)
                    if source_line is not None:
                        counts["lines"] += 1

                for number, row in sorted(
                        context["analyses"].items(),
                        key=lambda item: str(item[0])):
                    provenance_spools["coverage_analysis"].append(
                        _source_provenance_item(
                            "legacy_analysis", "coverage_analysis",
                            "{}:{}:{}".format(
                                project_name, context["file_path_hash"], number
                            ), row, raw_status=row.get("status"),
                            raw_is_draft=row.get("is_draft"),
                        )
                    )
                for number, row in sorted(
                        context.get("source_lines", {}).items(),
                        key=lambda item: str(item[0])):
                    provenance_spools["coverage_line_index"].append(
                        _source_provenance_item(
                            "line", "coverage_line_index",
                            "{}:{}:{}".format(
                                project_name, context["file_path_hash"], number
                            ), row,
                        )
                    )

            state = fetchone(connection, """
                SELECT * FROM coverage_project_state WHERE project_name=?
            """, (project_name,)) or {}
            if source_spool is not None:
                source_spool.append_project_state(project_name, state)
            provenance_spools["coverage_project_state"].append(
                _source_provenance_item(
                    "project_state", "coverage_project_state", project_name,
                    {"legacy_created_at": None,
                     "legacy_updated_at": state.get("updated_at"),
                     "raw_payload_sha256": _legacy_payload_hash(state)},
                )
            )
            fragment_offsets[project_name] = {
                "lines": (line_start, line_fragment_spool.tell()),
                "analyses": (analysis_start, analysis_fragment_spool.tell()),
            }

        for row in _iter_legacy_rows(connection, "coverage_background_jobs"):
            fact = _legacy_job_fact(row)
            if source_spool is not None:
                source_spool.append_job(fact)
            state = fact["state"]
            error = fact["error_message"]
            if state in ("queued", "running", "interrupted"):
                state = "interrupted"
                error = error or "legacy active job requires manual migration decision"
            array_spools["jobs"].append({
                "job_id": fact["job_id"], "project_name": fact["project_name"],
                "kind": fact["kind"], "state": state,
                "data_version": fact["data_version"], "error_message": error,
            })
            provenance_spools["coverage_background_jobs"].append(
                _source_provenance_item(
                    "job", "coverage_background_jobs", fact["job_id"], fact,
                    raw_status=fact.get("state"),
                )
            )
            counts["jobs"] += 1

        for project_name in project_names:
            array_spools["projects"].append(project_name)

        hasher = hashlib.sha256()
        hasher.update(b"{")
        fields = (
            ("analyses", array_spools["analyses"]),
            ("jobs", array_spools["jobs"]),
            ("legacy_provenance", provenance_spools),
            ("lines", array_spools["lines"]),
            ("project_data_versions", dict(sorted(data_versions.items()))),
            ("projects", array_spools["projects"]),
        )
        for index, (key, value) in enumerate(fields):
            if index:
                hasher.update(b",")
            hasher.update(_json_compact(key))
            hasher.update(b":")
            if key == "legacy_provenance":
                hasher.update(b"[")
                first_group = True
                for group in (
                        "coverage_analysis", "coverage_background_jobs",
                        "coverage_line_index", "coverage_project_state"):
                    spool = value[group]
                    if not spool.has_items:
                        continue
                    if not first_group:
                        hasher.update(b",")
                    spool.copy_to(hasher)
                    first_group = False
                hasher.update(b"]")
            elif isinstance(value, _JsonArraySpool):
                hasher.update(b"[")
                value.copy_to(hasher)
                hasher.update(b"]")
            else:
                hasher.update(_json_compact(value))
        hasher.update(b"}")

        project_fragments = {}
        for project_name in project_names:
            line_start, line_end = fragment_offsets[project_name]["lines"]
            analysis_start, analysis_end = fragment_offsets[project_name]["analyses"]
            fragment = hashlib.sha256()
            line_fragment_spool.seek(line_start)
            remaining = line_end - line_start
            while remaining > 0:
                chunk = line_fragment_spool.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("legacy line fragment spool ended early")
                fragment.update(chunk)
                remaining -= len(chunk)
            analysis_fragment_spool.seek(analysis_start)
            remaining = analysis_end - analysis_start
            while remaining > 0:
                chunk = analysis_fragment_spool.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise RuntimeError("legacy analysis fragment spool ended early")
                fragment.update(chunk)
                remaining -= len(chunk)
            project_fragments[project_name] = fragment.hexdigest()

        return {
            "semantic_hash": hasher.hexdigest(), "projects": project_names,
            "project_data_versions": dict(sorted(data_versions.items())),
            "source_line_facts": counts["lines"],
            "source_analysis_facts": counts["analyses"],
            "source_jobs": counts["jobs"],
            "project_fragments": project_fragments,
        }
    finally:
        line_fragment_spool.close()
        analysis_fragment_spool.close()
        for spool in array_spools.values():
            spool.close()
        for spool in provenance_spools.values():
            spool.close()


def _upsert_migration_checkpoint(connection, migration_id, checkpoint_key,
                                 phase, source_cursor="", semantic_fragment_hash="",
                                 target_counts=None, state="COMPLETED"):
    checkpoint_key_hash = _migration_checkpoint_key_hash(
        migration_id, checkpoint_key
    )
    payload = json.dumps(target_counts or {}, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    existing = fetchone(connection, """
        SELECT migration_id, checkpoint_key FROM coverage_migration_checkpoints
        WHERE checkpoint_key_hash=?
    """, (checkpoint_key_hash,))
    if existing and (
            str(existing.get("migration_id") or "") != str(migration_id) or
            str(existing.get("checkpoint_key") or "") != str(checkpoint_key)):
        raise RuntimeError("migration checkpoint hash collision")
    values = (phase, source_cursor, semantic_fragment_hash, payload, state, _now())
    cursor = connection.cursor()
    try:
        if existing:
            cursor.execute(adapt_sql(connection, """
                UPDATE coverage_migration_checkpoints
                SET phase=?, source_cursor=?, semantic_fragment_hash=?, target_counts=?,
                    state=?, updated_at=?
                WHERE checkpoint_key_hash=?
            """), values + (checkpoint_key_hash,))
        else:
            cursor.execute(adapt_sql(connection, """
                INSERT INTO coverage_migration_checkpoints(
                    migration_id, checkpoint_key, checkpoint_key_hash, phase, source_cursor,
                    semantic_fragment_hash, target_counts, migration_version,
                    state, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """), (migration_id, checkpoint_key, checkpoint_key_hash, phase,
                    source_cursor, semantic_fragment_hash, payload, state,
                    _now()))
    finally:
        cursor.close()


def _migration_checkpoint_done(connection, migration_id, checkpoint_key,
                               semantic_fragment_hash=""):
    checkpoint_key_hash = _migration_checkpoint_key_hash(
        migration_id, checkpoint_key
    )
    row = fetchone(connection, """
        SELECT migration_id, checkpoint_key, state, semantic_fragment_hash
        FROM coverage_migration_checkpoints
        WHERE checkpoint_key_hash=?
    """, (checkpoint_key_hash,))
    if row and (
            str(row.get("migration_id") or "") != str(migration_id) or
            str(row.get("checkpoint_key") or "") != str(checkpoint_key)):
        raise RuntimeError("migration checkpoint hash collision")
    return bool(row and str(row.get("state") or "") == "COMPLETED" and
                (not semantic_fragment_hash or
                 str(row.get("semantic_fragment_hash") or "") == semantic_fragment_hash))


def _legacy_context_fragment(context, project_name):
    hasher = hashlib.sha256()
    provenance_rows = []
    line_count = 0
    analysis_count = 0
    for number in sorted(context.get("lines") or {}):
        item = _semantic_line_item(dict(
            context["lines"][number], project_name=project_name,
            file_path_hash=context["file_path_hash"], file_path=context["file_path"],
        ))
        hasher.update(b"L" + _json_compact(item))
        line_count += 1
        source_line = context.get("source_lines", {}).get(number)
        if source_line is not None:
            provenance_rows.append({
                "entity_type": "line", "target_entity_id": None,
                "source_table": "coverage_line_index",
                "source_identity": "{}:{}:{}".format(
                    project_name, context["file_path_hash"], number
                ),
                "legacy_created_at": source_line.get("legacy_created_at"),
                "legacy_updated_at": source_line.get("legacy_updated_at"),
                "legacy_raw_status": None, "legacy_raw_is_draft": None,
                "raw_payload_sha256": source_line.get("raw_payload_sha256", ""),
            })
    for number in sorted(context.get("analyses") or {}):
        source_analysis = context["analyses"][number]
        item = _semantic_analysis_item(dict(
            source_analysis, project_name=project_name,
            file_path_hash=context["file_path_hash"], file_path=context["file_path"],
        ))
        hasher.update(b"A" + _json_compact(item))
        analysis_count += 1
        provenance_rows.append({
            "entity_type": "legacy_analysis", "target_entity_id": None,
            "source_table": "coverage_analysis",
            "source_identity": "{}:{}:{}".format(
                project_name, context["file_path_hash"], number
            ),
            "legacy_created_at": source_analysis.get("legacy_created_at"),
            "legacy_updated_at": source_analysis.get("legacy_updated_at"),
            "legacy_raw_status": source_analysis.get("status"),
            "legacy_raw_is_draft": source_analysis.get("is_draft"),
            "raw_payload_sha256": source_analysis.get("raw_payload_sha256", ""),
        })
    return hasher.hexdigest(), line_count, analysis_count, provenance_rows


def _persist_legacy_context(connection, project_name, scan_id, context,
                            migration_id, project_repo, line_repo, analysis_repo):
    files = project_repo.ensure_files(connection, scan_id, [{
        "repository_name": context["repository_name"],
        "file_path_hash": context["file_path_hash"],
        "file_path": context["file_path"],
        "source_file_name": context["source_file_name"],
    }])
    file_row = files[(context["repository_name"], context["file_path_hash"])]
    line_rows = line_repo.upsert_lines(
        connection, file_row["id"],
        [context["lines"][number] for number in sorted(context["lines"])],
        return_rows=True,
    )
    lines_by_number = {int(row["line_number"]): row for row in line_rows}
    provenance_rows = []
    for number, source_line in sorted(context.get("source_lines", {}).items()):
        line = lines_by_number.get(int(number))
        if not line:
            raise RuntimeError("migrated line identity was not returned")
        provenance_rows.append({
            "entity_type": "line", "target_entity_id": line["id"],
            "source_table": "coverage_line_index",
            "source_identity": "{}:{}:{}".format(
                project_name, context["file_path_hash"], number
            ),
            "legacy_created_at": source_line.get("legacy_created_at"),
            "legacy_updated_at": source_line.get("legacy_updated_at"),
            "legacy_raw_status": None, "legacy_raw_is_draft": None,
            "raw_payload_sha256": source_line.get("raw_payload_sha256", ""),
        })
    analysis_batch = []
    analysis_source_rows = []
    for number, source_analysis in sorted(context.get("analyses", {}).items()):
        line = lines_by_number.get(int(number))
        if not line:
            raise RuntimeError("analysis line identity was not returned")
        analysis_batch.append(dict(source_analysis, line_id=line["id"]))
        analysis_source_rows.append((line["id"], source_analysis))
    saved_analyses = analysis_repo.upsert_many(connection, analysis_batch)
    analyses_by_line = {int(row["line_id"]): row for row in saved_analyses}
    for line_id, source_analysis in analysis_source_rows:
        analysis = analyses_by_line.get(int(line_id))
        if not analysis:
            raise RuntimeError("bulk analysis upsert did not return line identity")
        provenance_rows.append({
            "entity_type": "legacy_analysis", "target_entity_id": analysis["id"],
            "source_table": "coverage_analysis",
            "source_identity": "{}:{}:{}".format(
                project_name, context["file_path_hash"], source_analysis["line_number"]
            ),
            "legacy_created_at": source_analysis.get("legacy_created_at"),
            "legacy_updated_at": source_analysis.get("legacy_updated_at"),
            "legacy_raw_status": source_analysis.get("status"),
            "legacy_raw_is_draft": source_analysis.get("is_draft"),
            "raw_payload_sha256": source_analysis.get("raw_payload_sha256", ""),
        })
    _upsert_legacy_provenance_many(connection, migration_id, provenance_rows)
    return file_row, lines_by_number, saved_analyses


# These are the only objects that may exist before the first VNext business
# DDL.  The list is intentionally explicit: a prefix/suffix rule would allow
# a legacy table or an operator-created staging table to slip through the
# Empty Target gate.
TARGET_BOOTSTRAP_TABLES = frozenset((
    "coverage_schema_meta", "coverage_schema_migrations",
    "coverage_migration_checkpoints",
))
VNEXT_BUSINESS_TABLES = frozenset((
    "coverage_projects", "coverage_scans", "coverage_scan_repositories",
    "coverage_reports", "coverage_files", "coverage_lines",
    "coverage_analyses", "coverage_project_state", "coverage_file_state",
    "coverage_background_jobs", "coverage_incremental_results",
    "coverage_legacy_provenance", "coverage_repositories",
    "coverage_repository_aliases", "coverage_repository_resources",
    "coverage_analysis_records", "coverage_analysis_blocks",
    "coverage_inheritance_groups", "coverage_analysis_line_links",
    "coverage_inheritance_decisions", "coverage_inheritance_rejections",
    "coverage_repository_resource_locks", "coverage_import_artifacts",
    "coverage_import_checkpoints", "coverage_import_failures",
))


def _database_table_names(connection):
    """Return user tables without executing arbitrary table-name SQL."""
    if is_sqlite(connection):
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return sorted(str(row[0]) for row in rows)
    rows = fetchall(connection, """
        SELECT TABLE_NAME FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME
    """)
    return sorted(str(row.get("TABLE_NAME") or row.get("table_name") or "")
                  for row in rows if row.get("TABLE_NAME") or row.get("table_name"))


def _safe_table_count(connection, table_name):
    if not table_name or not table_name.replace("_", "").isalnum():
        raise ValueError("unsafe target table name")
    return int((fetchone(connection, "SELECT COUNT(*) AS total FROM {}".format(
        table_name)) or {}).get("total") or 0)


def _target_fingerprint(connection):
    try:
        value = fingerprint_connection(connection)
    except Exception:
        value = {}
    return value if isinstance(value, dict) else {}


def assert_empty_vnext_target(connection, migration_id="coverage-vnext-core-v2",
                              allow_initialized_schema=False):
    """Fail closed unless the target is empty or a resumable empty schema.

    This function is deliberately independent from ``apply_schema`` so direct
    runners and rehearsal tools can call the same gate.  It performs no
    business DDL/DML.  A schema whose business tables already contain rows is
    never accepted, even when its migration ledger claims to be incomplete.
    """
    tables = _database_table_names(connection)
    unknown = sorted(set(tables) - TARGET_BOOTSTRAP_TABLES - VNEXT_BUSINESS_TABLES)
    if unknown:
        raise RuntimeError(
            "MIGRATION_TARGET_NOT_EMPTY:unknown_tables=" + ",".join(unknown)
        )
    counts = {table: _safe_table_count(connection, table) for table in tables}
    business_counts = {
        table: counts.get(table, 0) for table in VNEXT_BUSINESS_TABLES
        if table in counts
    }
    nonempty_business = sorted(
        table for table, count in business_counts.items() if int(count) > 0
    )
    if nonempty_business:
        raise RuntimeError(
            "MIGRATION_TARGET_NOT_EMPTY:business_rows=" +
            ",".join(nonempty_business)
        )

    ledger = None
    if "coverage_schema_migrations" in tables:
        ledger = fetchone(connection, """
            SELECT * FROM coverage_schema_migrations
            WHERE migration_id=? ORDER BY started_at DESC LIMIT 1
        """, (str(migration_id),))
    business_schema_present = bool(set(tables) & VNEXT_BUSINESS_TABLES)
    if business_schema_present and not allow_initialized_schema:
        raise RuntimeError(
            "MIGRATION_TARGET_NOT_EMPTY:business_schema_present"
        )
    if business_schema_present and allow_initialized_schema and not ledger:
        raise RuntimeError(
            "MIGRATION_TARGET_NOT_EMPTY:business_schema_untracked"
        )

    runtime = _target_fingerprint(connection)
    inventory_payload = {
        "tables": tables, "counts": counts,
        "migration_id": str(migration_id),
    }
    inventory_hash = hashlib.sha256(json.dumps(
        inventory_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=_semantic_json_default,
    ).encode("utf-8")).hexdigest()
    result = "EMPTY" if not tables else (
        "BOOTSTRAP_ONLY" if not business_schema_present else "RESUMABLE_EMPTY_SCHEMA"
    )
    return {
        "status": "PASSED", "result": result, "migration_id": str(migration_id),
        "tables": tables, "counts": counts,
        "unknown_tables": unknown,
        "runtime_fingerprint": runtime,
        "database": str(runtime.get("database") or ""),
        "table_inventory_hash": inventory_hash,
        "ledger_state": str((ledger or {}).get("state") or ""),
    }


def assert_applied_vnext_target(connection, migration_id="coverage-vnext-core-v2",
                                ddl_sha256=""):
    """Validate an already-applied schema before an idempotent re-run.

    ``assert_empty_vnext_target`` is intentionally strict: it must reject a
    populated or untracked business schema before the first migration.  The
    normal migration lifecycle also calls ``apply_schema`` after business
    rows exist to prove idempotency, however.  Treating that legitimate
    post-APPLIED check as an empty-target preflight makes every real MariaDB
    rehearsal fail with ``MIGRATION_TARGET_NOT_EMPTY``.

    This separate check preserves the boundary: it only accepts the exact
    applied ledger state, the matching DDL checksum, the known VNext table
    namespace, and a recorded core schema marker.  It never makes a populated
    database eligible for an initial migration.
    """
    tables = _database_table_names(connection)
    unknown = sorted(set(tables) - TARGET_BOOTSTRAP_TABLES - VNEXT_BUSINESS_TABLES)
    if unknown:
        raise RuntimeError(
            "MIGRATION_TARGET_NOT_EMPTY:unknown_tables=" + ",".join(unknown)
        )
    ledger = fetchone(connection, """
        SELECT * FROM coverage_schema_migrations
        WHERE migration_id=? ORDER BY started_at DESC LIMIT 1
    """, (str(migration_id),)) if "coverage_schema_migrations" in tables else None
    if not ledger or str(ledger.get("state") or "").upper() != "APPLIED":
        raise RuntimeError("MIGRATION_TARGET_NOT_EMPTY:applied_ledger_missing")
    recorded_sha = str(ledger.get("ddl_sha256") or "")
    if ddl_sha256 and recorded_sha != str(ddl_sha256):
        raise ValueError("schema migration checksum changed after APPLIED state")
    marker = fetchone(connection, """
        SELECT schema_version, migration_id
        FROM coverage_schema_meta WHERE schema_key=?
    """, ("coverage_vnext_core",)) if "coverage_schema_meta" in tables else None
    if not marker or int(marker.get("schema_version") or 0) != 1:
        raise RuntimeError("MIGRATION_TARGET_NOT_EMPTY:applied_schema_marker_missing")
    if str(marker.get("migration_id") or "") not in ("", str(migration_id)):
        raise RuntimeError("MIGRATION_TARGET_NOT_EMPTY:applied_schema_marker_mismatch")
    counts = {table: _safe_table_count(connection, table) for table in tables}
    runtime = _target_fingerprint(connection)
    inventory_payload = {
        "tables": tables, "counts": counts,
        "migration_id": str(migration_id), "state": "APPLIED",
    }
    inventory_hash = hashlib.sha256(json.dumps(
        inventory_payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=_semantic_json_default,
    ).encode("utf-8")).hexdigest()
    return {
        "status": "PASSED", "result": "APPLIED_SCHEMA",
        "migration_id": str(migration_id), "tables": tables,
        "counts": counts, "unknown_tables": unknown,
        "runtime_fingerprint": runtime,
        "database": str(runtime.get("database") or ""),
        "table_inventory_hash": inventory_hash,
        "ledger_state": "APPLIED", "ddl_sha256": recorded_sha,
    }


def _record_target_preflight(connection, migration_id, preflight, state="PREFLIGHTED",
                             ddl_sha256="", release_sha=""):
    """Persist the exact preflight evidence in the bootstrap ledger."""
    if not _table_exists(connection, "coverage_schema_migrations"):
        raise RuntimeError("migration ledger must exist before preflight evidence")
    now = _now()
    runtime = preflight.get("runtime_fingerprint") or {}
    runtime_key = str(runtime.get("runtime_key") or "")
    database = str(preflight.get("database") or runtime.get("database") or "")
    fields = (str(migration_id), "coverage_vnext_core", ddl_sha256 or "", state,
              now, release_sha or "", database, runtime_key,
              str(preflight.get("table_inventory_hash") or ""),
              str(preflight.get("result") or ""), now)
    existing = fetchone(connection, """
        SELECT migration_id FROM coverage_schema_migrations WHERE migration_id=?
    """, (str(migration_id),))
    cursor = connection.cursor()
    try:
        if existing:
            cursor.execute(adapt_sql(connection, """
                UPDATE coverage_schema_migrations
                SET schema_key=?, ddl_sha256=?, state=?, started_at=?,
                    release_sha=?, target_database=?, target_runtime_fingerprint=?,
                    target_table_inventory_hash=?, target_emptiness_result=?,
                    target_preflight_at=?
                WHERE migration_id=?
            """), (fields[1], fields[2], fields[3], fields[4], fields[5],
                    fields[6], fields[7], fields[8], fields[9], fields[10],
                    fields[0]))
        else:
            cursor.execute(adapt_sql(connection, """
                INSERT INTO coverage_schema_migrations(
                    migration_id, schema_key, from_version, to_version,
                    ddl_sha256, state, started_at, release_sha,
                    target_database, target_runtime_fingerprint,
                    target_table_inventory_hash, target_emptiness_result,
                    target_preflight_at
                ) VALUES (?, ?, 0, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """), fields)
    finally:
        cursor.close()
def capture_legacy_snapshot(connection):
    """Normalize legacy facts without changing their source semantics."""
    analysis = [_legacy_analysis_fact(row) for row in _iter_legacy_rows(
        connection, "coverage_analysis"
    )]
    lines = [_legacy_line_fact(row) for row in _iter_legacy_rows(
        connection, "coverage_line_index"
    )]
    projects = {}
    project_metadata = {}
    for row in _iter_legacy_rows(connection, "coverage_project_state"):
        name = str(row.get("project_name") or "")
        projects[name] = int(row.get("data_version") or 0)
        project_metadata[name] = {
            "file_state_version": int(row.get("file_state_version") or 0),
            "current_scan_key": str(row.get("current_scan_key") or ""),
            "updated_at": row.get("updated_at"),
            "raw_payload_sha256": _legacy_payload_hash(row),
        }
    jobs = []
    for row in _iter_legacy_rows(connection, "coverage_background_jobs"):
        jobs.append(_legacy_job_fact(row))
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


def _stream_vnext_semantic_hash(connection, batch_size=LEGACY_STREAM_BATCH_SIZE):
    """Hash the target semantic contract without materializing target facts.

    ``capture_vnext_semantic_snapshot`` remains a small-fixture diagnostic
    helper, but the migration gate must not use it for production-sized
    targets.  The query order and selected fields below intentionally mirror
    that helper and ``_stream_legacy_semantic_hash`` so the resulting hash is
    byte-for-byte comparable while rows are consumed in bounded batches.
    """
    hasher = hashlib.sha256()
    hasher.update(b"{")
    first_field = True

    def field_prefix(name):
        nonlocal first_field
        if not first_field:
            hasher.update(b",")
        hasher.update(_json_compact(name))
        hasher.update(b":")
        first_field = False

    def stream_array(rows, transform):
        hasher.update(b"[")
        first_item = True
        for row in rows:
            if not first_item:
                hasher.update(b",")
            hasher.update(_json_compact(transform(row)))
            first_item = False
        hasher.update(b"]")

    field_prefix("analyses")
    stream_array(iter_rows(connection, """
            SELECT p.project_name, f.file_path_hash, f.file_path, l.line_number,
                   a.status, a.is_draft, a.reviewer, a.coverage_method,
                   a.uncovered_reason, a.comment
            FROM coverage_analyses a
            JOIN coverage_lines l ON l.id = a.line_id
            JOIN coverage_files f ON f.id = l.file_id
            JOIN coverage_scans s ON s.id = f.scan_id
            JOIN coverage_projects p ON p.id = s.project_id
            ORDER BY p.project_name, f.file_path_hash, l.line_number
        """, batch_size=batch_size), lambda row: {
            key: row.get(key) for key in (
                "project_name", "file_path_hash", "file_path", "line_number",
                "status", "is_draft", "reviewer", "coverage_method",
                "uncovered_reason", "comment",
            )
        })

    field_prefix("jobs")

    def normalize_job(row):
        state = row.get("state")
        error_message = row.get("error_message") or ""
        if state in ("queued", "running", "interrupted"):
            state = "interrupted"
            error_message = error_message or (
                "legacy active job requires manual migration decision"
            )
        return {
            "job_id": row.get("job_id"),
            "project_name": row.get("project_name"),
            "kind": row.get("kind"),
            "state": state,
            "data_version": row.get("data_version"),
            "error_message": error_message,
        }

    stream_array(iter_rows(connection, """
            SELECT j.job_id, p.project_name, j.kind, j.state,
                   COALESCE(j.data_version, 0) AS data_version,
                   COALESCE(j.error_message, '') AS error_message
            FROM coverage_background_jobs j
            LEFT JOIN coverage_projects p ON p.id = j.project_id
            ORDER BY j.job_id
        """, batch_size=batch_size), normalize_job)

    field_prefix("legacy_provenance")

    def normalize_provenance(row):
        item = {
            key: row.get(key) for key in (
                "target_entity_type", "source_table", "source_identity",
                "legacy_created_at", "legacy_updated_at", "legacy_raw_status",
                "legacy_raw_is_draft", "raw_payload_sha256",
            )
        }
        if item.get("legacy_raw_status") == "":
            item["legacy_raw_status"] = None
        return item

    if _table_exists(connection, "coverage_legacy_provenance"):
        provenance_rows = iter_rows(connection, """
                SELECT target_entity_type, source_table, source_identity,
                       legacy_created_at, legacy_updated_at,
                       legacy_raw_status, legacy_raw_is_draft,
                       raw_payload_sha256
                FROM coverage_legacy_provenance
                ORDER BY source_table, source_identity, target_entity_type
            """, batch_size=batch_size)
    else:
        provenance_rows = ()
    stream_array(provenance_rows, normalize_provenance)

    field_prefix("lines")
    stream_array(iter_rows(connection, """
            SELECT p.project_name, f.file_path_hash, f.file_path, l.line_number,
                   l.line_text, l.block_start_line, l.block_end_line, l.block_type,
                   l.function_name, l.function_hash, l.code_line_hash,
                   l.code_occurrence
            FROM coverage_lines l
            JOIN coverage_files f ON f.id = l.file_id
            JOIN coverage_scans s ON s.id = f.scan_id
            JOIN coverage_projects p ON p.id = s.project_id
            ORDER BY p.project_name, f.file_path_hash, l.line_number
        """, batch_size=batch_size), lambda row: {
            key: row.get(key) for key in (
                "project_name", "file_path_hash", "file_path", "line_number",
                "line_text", "block_start_line", "block_end_line", "block_type",
                "function_name", "function_hash", "code_line_hash",
                "code_occurrence",
            )
        })

    field_prefix("project_data_versions")
    hasher.update(b"{")
    first_project = True
    for row in iter_rows(connection, """
            SELECT p.project_name, s.data_version
            FROM coverage_project_state s
            JOIN coverage_projects p ON p.id = s.project_id
            ORDER BY p.project_name
        """, batch_size=batch_size):
        if not first_project:
            hasher.update(b",")
        hasher.update(_json_compact(str(row.get("project_name") or "")))
        hasher.update(b":")
        hasher.update(_json_compact(int(row.get("data_version") or 0)))
        first_project = False
    hasher.update(b"}")

    field_prefix("projects")
    stream_array(iter_rows(connection, """
            SELECT project_name FROM coverage_projects ORDER BY project_name
        """, batch_size=batch_size), lambda row: row.get("project_name"))
    hasher.update(b"}")
    return hasher.hexdigest()


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
    provenance_key_hash = _legacy_provenance_key_hash(
        migration_id, entity_type, target_entity_id, source_table
    )
    values = (
        migration_id, entity_type, int(target_entity_id or 0), source_table,
        provenance_key_hash, str(source_identity or ""),
        row.get("legacy_created_at"),
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
        if str(existing.get("raw_payload_sha256") or "") != values[10]:
            raise ValueError("legacy provenance input changed on idempotent rerun")
        return int(existing.get("id") or 0)
    cursor = connection.cursor()
    cursor.execute(adapt_sql(connection, """
        INSERT INTO coverage_legacy_provenance(
            migration_id, target_entity_type, target_entity_id, source_table,
            provenance_key_hash, source_identity, legacy_created_at, legacy_updated_at,
            legacy_raw_status, legacy_raw_is_draft, raw_payload_sha256,
            raw_payload, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """), values)
    result = insert_id(cursor)
    cursor.close()
    return result


def _upsert_legacy_provenance_many(connection, migration_id, records):
    """Persist provenance in bounded batches without losing idempotency.

    The first migration implementation called ``_upsert_legacy_provenance``
    once per line and once per analysis.  That made a production-sized
    rehearsal perform a SELECT/INSERT round trip for every fact even though
    the target transaction was already authoritative.  Resolve only the
    current bounded batch of identities, validate their immutable hashes, and
    insert only missing rows with ``executemany``.  Never materialize the
    complete target provenance ledger just to process one file.
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

    existing = {}
    keys = sorted(normalized)
    key_chunk_size = bind_chunk_size(
        connection, parameter_width=3, reserved=1, maximum=200
    )
    for offset in range(0, len(keys), key_chunk_size):
        key_chunk = keys[offset:offset + key_chunk_size]
        clauses = []
        params = [migration_id]
        for entity_type, entity_id, source_table in key_chunk:
            clauses.append(
                "(target_entity_type=? AND target_entity_id=? AND source_table=?)"
            )
            params.extend((entity_type, int(entity_id), source_table))
        existing_rows = fetchall(connection, """
            SELECT target_entity_type, target_entity_id, source_table,
                   raw_payload_sha256
            FROM coverage_legacy_provenance
            WHERE migration_id=? AND ({})
        """.format(" OR ".join(clauses)), params)
        for row in existing_rows:
            key = (
                str(row.get("target_entity_type") or ""),
                int(row.get("target_entity_id") or 0),
                str(row.get("source_table") or ""),
            )
            existing[key] = str(row.get("raw_payload_sha256") or "")
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
            _legacy_provenance_key_hash(
                migration_id, key[0], key[1], key[2]
            ),
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
                    source_table, provenance_key_hash, source_identity,
                    legacy_created_at,
                    legacy_updated_at, legacy_raw_status, legacy_raw_is_draft,
                    raw_payload_sha256, raw_payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """), inserts)
        finally:
            cursor.close()
    return {"inserted": len(inserts), "existing": existing_count}


def create_sqlite_schema(connection):
    connection.executescript(SQLITE_SCHEMA)
    # Domain tables include the migration ledger.  Create them before applying
    # additive columns so the helper also works for a brand-new SQLite target.
    connection.executescript(SQLITE_DOMAIN_SCHEMA)
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
    if not _column_exists(
            connection, "coverage_legacy_provenance", "provenance_key_hash"):
        connection.execute(
            "ALTER TABLE coverage_legacy_provenance "
            "ADD COLUMN provenance_key_hash TEXT NOT NULL DEFAULT ''"
        )
    if not _column_exists(
            connection, "coverage_import_failures", "failure_key_hash"):
        connection.execute(
            "ALTER TABLE coverage_import_failures "
            "ADD COLUMN failure_key_hash TEXT NOT NULL DEFAULT ''"
        )
    _ensure_legacy_provenance_key_hash(connection)
    for ensure_key_hash in (
            _ensure_incremental_result_key_hash,
            _ensure_import_failure_key_hash):
        ensure_key_hash(connection)
    _ensure_runtime_indexes(connection)
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

    existing_before_preflight = None
    if _table_exists(connection, "coverage_schema_migrations"):
        existing_before_preflight = fetchone(connection, """
            SELECT * FROM coverage_schema_migrations WHERE migration_id=?
        """, (migration_id,))
    existing_state = str(
        (existing_before_preflight or {}).get("state") or ""
    ).upper()
    if existing_state == "APPLIED":
        if str(existing_before_preflight.get("ddl_sha256") or "") != ddl_sha256:
            raise ValueError("schema migration checksum changed after APPLIED state")
        preflight = assert_applied_vnext_target(
            connection, migration_id=migration_id, ddl_sha256=ddl_sha256,
        )
        _ensure_migration_checkpoint_table(connection)
        _ensure_runtime_indexes(connection)
        connection.commit()
        return {"status": "PASSED", "migration_id": migration_id,
                "idempotent": True, "ddl_sha256": ddl_sha256,
                "target_preflight": preflight}
    allow_initialized = existing_state in {"PREFLIGHTED", "STARTED", "FAILED"}
    preflight = assert_empty_vnext_target(
        connection, migration_id=migration_id,
        allow_initialized_schema=allow_initialized,
    )

    if is_sqlite(connection):
        # Create only the bootstrap ledger before any business table.  This
        # makes the preflight evidence durable even if the following schema
        # construction is interrupted.
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS coverage_schema_migrations (
                migration_id TEXT PRIMARY KEY, schema_key TEXT NOT NULL,
                from_version INTEGER NOT NULL DEFAULT 0, to_version INTEGER NOT NULL,
                ddl_sha256 TEXT NOT NULL, state TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT,
                release_sha TEXT NOT NULL DEFAULT '',
                error_class TEXT NOT NULL DEFAULT '',
                target_database TEXT NOT NULL DEFAULT '',
                target_runtime_fingerprint TEXT NOT NULL DEFAULT '',
                target_table_inventory_hash TEXT NOT NULL DEFAULT '',
                target_emptiness_result TEXT NOT NULL DEFAULT '',
                target_preflight_at TEXT
            );
            CREATE TABLE IF NOT EXISTS coverage_schema_meta (
                schema_key TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                applied_at TEXT NOT NULL, release_sha TEXT NOT NULL DEFAULT '',
                migration_id TEXT NOT NULL DEFAULT ''
            );
        """)
        _record_target_preflight(
            connection, migration_id, preflight, state="PREFLIGHTED",
            ddl_sha256=ddl_sha256, release_sha=release_sha,
        )
        connection.commit()
        create_sqlite_schema(connection)
        _ensure_migration_checkpoint_table(connection)
        existing = fetchone(connection, """
            SELECT * FROM coverage_schema_migrations WHERE migration_id=?
        """, (migration_id,))
        if existing and str(existing.get("state") or "") == "APPLIED":
            if str(existing.get("ddl_sha256") or "") != ddl_sha256:
                raise ValueError("schema migration checksum changed after APPLIED state")
            return {"status": "PASSED", "migration_id": migration_id,
                    "idempotent": True, "ddl_sha256": ddl_sha256,
                    "target_preflight": preflight}
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
                "idempotent": bool(existing and str(existing.get("state") or "").upper() == "APPLIED"),
                "ddl_sha256": ddl_sha256,
                "target_preflight": preflight}

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
            target_database VARCHAR(128) NOT NULL DEFAULT '',
            target_runtime_fingerprint VARCHAR(255) NOT NULL DEFAULT '',
            target_table_inventory_hash CHAR(64) NOT NULL DEFAULT '',
            target_emptiness_result VARCHAR(64) NOT NULL DEFAULT '',
            target_preflight_at DATETIME NULL,
            PRIMARY KEY (migration_id)
        )
    """
    cursor = connection.cursor()
    cursor.execute(adapt_sql(connection, ledger_ddl))
    cursor.close()
    connection.commit()
    # Older Candidate ledgers may predate the evidence columns.  Upgrade only
    # the bootstrap ledger before recording the new preflight; this is not a
    # business-table migration and remains additive/idempotent.
    for column_name, definition in (
        ("target_database", "VARCHAR(128) NOT NULL DEFAULT ''"),
        ("target_runtime_fingerprint", "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("target_table_inventory_hash", "CHAR(64) NOT NULL DEFAULT ''"),
        ("target_emptiness_result", "VARCHAR(64) NOT NULL DEFAULT ''"),
        ("target_preflight_at", "DATETIME NULL"),
    ):
        if _column_exists(connection, "coverage_schema_migrations", column_name):
            continue
        cursor = connection.cursor()
        try:
            cursor.execute(adapt_sql(connection,
                                     "ALTER TABLE coverage_schema_migrations "
                                     "ADD COLUMN {} {}".format(column_name, definition)))
        finally:
            cursor.close()
    _record_target_preflight(
        connection, migration_id, preflight, state="PREFLIGHTED",
        ddl_sha256=ddl_sha256, release_sha=release_sha,
    )
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
    _ensure_migration_checkpoint_table(connection)
    connection.commit()
    existing_migration = fetchone(connection, """
        SELECT * FROM coverage_schema_migrations WHERE migration_id = ?
    """, (migration_id,))
    if existing_migration and str(existing_migration.get("state") or "") == "APPLIED":
        if str(existing_migration.get("ddl_sha256") or "") != ddl_sha256:
            raise ValueError("schema migration checksum changed after APPLIED state")
        return {"status": "PASSED", "migration_id": migration_id,
                "idempotent": True, "ddl_sha256": ddl_sha256,
                "target_preflight": preflight}

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
            try:
                cursor.execute(adapt_sql(connection, statement))
            except Exception as exc:
                compact = " ".join(str(statement).split())
                raise RuntimeError(
                    "DDL statement failed: {}".format(compact[:2000])
                ) from exc
            finally:
                cursor.close()
        # Existing Candidate databases are upgraded through information_schema
        # checks rather than unsafe ADD COLUMN IF NOT EXISTS (unsupported by
        # MariaDB 5.5).  Fresh targets get the columns from the DDL above.
        additions = {
            "coverage_schema_meta": [
                ("migration_id", "VARCHAR(128) NOT NULL DEFAULT ''"),
            ],
            "coverage_legacy_provenance": [
                ("provenance_key_hash", "CHAR(64) NOT NULL DEFAULT ''"),
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
        _ensure_legacy_provenance_key_hash(connection)
        _ensure_incremental_result_key_hash(connection)
        _ensure_import_failure_key_hash(connection)
        _ensure_runtime_indexes(connection)
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
            "idempotent": False, "ddl_sha256": ddl_sha256,
            "target_preflight": preflight}


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


def _migrate_legacy_materialized(source_connection, target_connection, anomaly_path=None,
                                  release_sha="", migration_id=None):
    """Compatibility implementation retained only for historical diagnostics."""
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
                    [context["lines"][number] for number in sorted(context["lines"])],
                    return_rows=True,
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

    target_hash = _stream_vnext_semantic_hash(target_connection)
    source_hash = semantic_hash(source_semantic)
    result = {
        "status": "PASSED",
        "source_projects": len(source["projects"]),
        "source_line_facts": len(source["lines"]),
        "source_analysis_facts": len(source["analyses"]),
        "source_jobs": len(source["jobs"]),
        "anomalies": anomalies,
        "source_semantic_hash": source_hash,
        "target_semantic_hash": target_hash,
        "authoritative_semantic_match": source_hash == target_hash,
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


def _migrate_legacy_with_source_spool(source_connection, target_connection,
                                      source_spool, anomaly_path=None,
                                      release_sha="", migration_id=None):
    """Stream a read-only legacy source into a non-published VNext target.

    Source facts are read in bounded file-hash groups.  Each target file/job
    batch commits its business rows and its durable checkpoint together, while
    CURRENT and sealed Scan state are withheld until the final semantic gate.
    """
    if source_connection is target_connection:
        raise ValueError("legacy source and VNext target must be separate")
    _ensure_migration_checkpoint_table(target_connection)
    target_connection.commit()
    project_names = _project_names(source_connection)
    data_versions = _legacy_project_data_versions(source_connection, project_names)
    source_descriptor = _stream_legacy_semantic_hash(
        source_connection, project_names, data_versions,
        source_spool=source_spool,
    )
    migration_id = migration_id or "legacy-v3-{}".format(
        source_descriptor["semantic_hash"][:32]
    )
    anomalies = []
    project_repo = ProjectRepository()
    line_repo = LineIndexRepository()
    analysis_repo = AnalysisRepository()
    state_repo = ProjectStateRepository()
    file_state_repo = FileStateRepository()
    job_repo = JobRepository()
    scan_ids = {}
    project_ids = {}

    def setup_project(project_name):
        project_fragment = source_descriptor["project_fragments"].get(project_name, "")
        scan_key = hashlib.sha256(_json_compact({
            "project": project_name, "source": "legacy_migrated_v3",
            "fragment": project_fragment,
        })).hexdigest()
        with transaction(target_connection) as conn:
            project = project_repo.ensure_project(conn, project_name)
            scan = project_repo.create_scan(
                conn, project["id"], scan_key, "legacy_migrated", "full",
                status="building", legacy_migrated=1,
            )
            if str(scan.get("status") or "").lower() in {"ready", "sealed"}:
                project_ids[project_name] = int(project["id"])
                scan_ids[project_name] = int(scan["id"])
                return
            project_repo.upsert_repository_snapshot(
                conn, scan["id"], "", verified=0, provenance="legacy_migration_v3"
            )
            project_repo.bind_report(
                conn, scan["id"], "legacy_{}".format(scan_key[:16]),
                source_signature="legacy_migration_v3", sidecar_schema=0,
            )
            state_repo.ensure(
                conn, project["id"], current_scan_id=None,
                data_version=int(data_versions.get(project_name, 0)),
            )
            project_ids[project_name] = int(project["id"])
            scan_ids[project_name] = int(scan["id"])

    for project_name in project_names:
        setup_project(project_name)
        scan_id = scan_ids[project_name]
        project_id = project_ids[project_name]
        project_fragment = source_descriptor["project_fragments"].get(project_name, "")
        project_checkpoint = "project:{}".format(project_name)
        for context in source_spool.iter_contexts(project_name):
            fragment, line_count, analysis_count, _ = _legacy_context_fragment(
                context, project_name
            )
            for number in context.get("missing_file_path_lines", []):
                anomalies.append({
                    "type": "missing_file_path", "project_name": project_name,
                    "file_path_hash": context["file_path_hash"], "line_number": number,
                })
            if context.get("path_conflict"):
                for number, paths in sorted(context.get("path_candidates", {}).items()):
                    if len(paths) > 1:
                        anomalies.append({
                            "type": "path_conflict", "project_name": project_name,
                            "file_path_hash": context["file_path_hash"],
                            "line_number": number, "paths": paths,
                        })
            for number in context.get("ambiguous_analysis_lines", []):
                anomalies.append({
                    "type": "analysis_path_ambiguous", "project_name": project_name,
                    "file_path_hash": context["file_path_hash"], "line_number": number,
                    "paths": context.get("path_candidates", {}).get(number, []),
                })
            for number in context.get("missing_line_index_lines", []):
                anomalies.append({
                    "type": "missing_line_index_context", "project_name": project_name,
                    "file_path_hash": context["file_path_hash"], "line_number": number,
                })
            checkpoint_key = "file:{}:{}:{}".format(
                project_name, context["file_path_hash"], context["file_path"]
            )
            if _migration_checkpoint_done(
                    target_connection, migration_id, checkpoint_key, fragment):
                continue
            with transaction(target_connection) as conn:
                _persist_legacy_context(
                    conn, project_name, scan_id, context, migration_id,
                    project_repo, line_repo, analysis_repo,
                )
                _upsert_migration_checkpoint(
                    conn, migration_id, checkpoint_key, "FILE", context["file_path"],
                    fragment, {"lines": line_count, "analyses": analysis_count},
                )

        with transaction(target_connection) as conn:
            project_metadata = source_spool.project_state(project_name)
            _upsert_legacy_provenance_many(conn, migration_id, [{
                "entity_type": "project_state", "target_entity_id": project_id,
                "source_table": "coverage_project_state", "source_identity": project_name,
                "legacy_created_at": None,
                "legacy_updated_at": project_metadata.get("updated_at"),
                "legacy_raw_status": None, "legacy_raw_is_draft": None,
                "raw_payload_sha256": _legacy_payload_hash(project_metadata),
            }])
            file_state_repo.rebuild_scan(
                conn, scan_id, int(data_versions.get(project_name, 0)), None
            )
            _upsert_migration_checkpoint(
                conn, migration_id, project_checkpoint, "PROJECT",
                project_name, project_fragment,
                {"scan_id": scan_id, "project_id": project_id}, state="READY_TO_PUBLISH",
            )

    # Jobs are independent keyset batches.  Active legacy work is retained as
    # an explicit interrupted/manual-decision fact, never silently requeued.
    for old_job in source_spool.iter_jobs():
        project = project_ids.get(old_job["project_name"])
        if not project:
            anomalies.append({"type": "orphan_job", "job_id": old_job["job_id"]})
            continue
        if old_job["state"] in ("queued", "running", "interrupted"):
            anomalies.append({
                "type": "active_job_requires_decision", "job_id": old_job["job_id"],
                "legacy_state": old_job["state"],
            })
        job_checkpoint = "job:{}".format(old_job["job_id"])
        job_fragment = hashlib.sha256(_json_compact(old_job)).hexdigest()
        if _migration_checkpoint_done(
                target_connection, migration_id, job_checkpoint, job_fragment):
            continue
        state = old_job["state"]
        if state in ("queued", "running", "interrupted"):
            state = "interrupted"
        with transaction(target_connection) as conn:
            job_repo.upsert(conn, {
                "job_id": old_job["job_id"], "project_id": project,
                "kind": old_job["kind"], "state": state, "progress": 0,
                "input_payload": json.dumps(old_job, sort_keys=True,
                                             default=_semantic_json_default),
                "error_message": old_job["error_message"] if state != "interrupted"
                else "legacy active job requires manual migration decision",
                "data_version": old_job["data_version"],
                "handler_version": "legacy-migration-v3",
                "legacy_raw_percent": old_job.get("legacy_raw_percent"),
                "legacy_percent_unit": old_job.get("legacy_percent_unit", ""),
            })
            target_entity_id = int(hashlib.sha256(
                old_job["job_id"].encode("utf-8")
            ).hexdigest()[:15], 16)
            _upsert_legacy_provenance_many(conn, migration_id, [{
                "entity_type": "job", "target_entity_id": target_entity_id,
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
            _upsert_migration_checkpoint(
                conn, migration_id, job_checkpoint, "JOB", old_job["job_id"],
                job_fragment, {"job_id": old_job["job_id"]},
            )

    # Keep the final zero-loss gate bounded on the target as well as on the
    # legacy source.  The diagnostic snapshot helper is intentionally retained
    # for small fixtures, but must not turn a production-sized target into a
    # resident Python object before publication.
    target_hash = _stream_vnext_semantic_hash(target_connection)
    result = {
        "status": "FAILED", "source_projects": len(project_names),
        "source_line_facts": source_descriptor["source_line_facts"],
        "source_analysis_facts": source_descriptor["source_analysis_facts"],
        "source_jobs": source_descriptor["source_jobs"], "anomalies": anomalies,
        "source_semantic_hash": source_descriptor["semantic_hash"],
        "target_semantic_hash": target_hash,
        "authoritative_semantic_match": source_descriptor["semantic_hash"] == target_hash,
        "release_sha": release_sha or "", "migration_id": migration_id,
        "captured_at": utc_iso(), "checkpointed": True,
    }
    if source_descriptor["semantic_hash"] != target_hash:
        result["error_class"] = "semantic_zero_loss_gate_failed"
        if anomaly_path:
            parent = os.path.dirname(os.path.abspath(anomaly_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(anomaly_path, "w", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
        return result

    # Publication is the final transaction and occurs only after the
    # authoritative semantic zero-loss gate. A process kill before this point
    # leaves only building Scans and durable file/job checkpoints.
    with transaction(target_connection) as conn:
        for project_name in project_names:
            project_id = project_ids[project_name]
            scan_id = scan_ids[project_name]
            state_repo.set_current_scan(conn, project_id, scan_id)
            state_repo.mark_ready(conn, project_id, int(data_versions.get(project_name, 0)))
            current = project_repo.get_scan(conn, scan_id)
            if str(current.get("status") or "").lower() in {"building", "importing", "constructing"}:
                project_repo.seal_scan(conn, scan_id)
            _upsert_migration_checkpoint(
                conn, migration_id, "project:{}".format(project_name), "PUBLISHED",
                project_name,
                source_descriptor["project_fragments"].get(project_name, ""),
                {"scan_id": scan_id, "project_id": project_id}, state="PUBLISHED",
            )
    result["status"] = "PASSED"
    if anomaly_path:
        parent = os.path.dirname(os.path.abspath(anomaly_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(anomaly_path, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
    return result


def migrate_legacy(source_connection, target_connection, anomaly_path=None,
                   release_sha="", migration_id=None):
    """Migrate a read-only legacy source using one replayable source pass."""
    source_spool = _LegacySourceSpool()
    try:
        return _migrate_legacy_with_source_spool(
            source_connection, target_connection, source_spool,
            anomaly_path=anomaly_path, release_sha=release_sha,
            migration_id=migration_id,
        )
    finally:
        source_spool.close()


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
            first_target_hash = _stream_vnext_semantic_hash(target)
            second = migrate_legacy(source, target, args.anomaly_path or None, args.release_sha)
            second_target_hash = _stream_vnext_semantic_hash(target)
            result = {
                "status": "PASSED" if (
                    first.get("authoritative_semantic_match")
                    and second.get("authoritative_semantic_match")
                    and first_target_hash == second_target_hash
                ) else "FAILED",
                "first_run": first,
                "second_run": second,
                "first_target_semantic_hash": first_target_hash,
                "second_target_semantic_hash": second_target_hash,
                "idempotent": first_target_hash == second_target_hash,
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
