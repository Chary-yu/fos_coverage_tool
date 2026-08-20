"""Legacy-to-VNext migration runner and semantic integrity snapshots."""

from __future__ import print_function

import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime

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
from app.db.repositories.base import fetchall, fetchone, is_sqlite
from app.db.transaction import transaction


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
    pending_total INTEGER NOT NULL DEFAULT 0, data_version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL, PRIMARY KEY(scan_id, file_id)
);
CREATE TABLE IF NOT EXISTS coverage_background_jobs (
    job_id TEXT PRIMARY KEY, project_id INTEGER, scan_id INTEGER, kind TEXT NOT NULL,
    state TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, input_payload TEXT NOT NULL,
    result_path TEXT NOT NULL DEFAULT '', error_message TEXT, data_version INTEGER,
    heartbeat_at TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    updated_at TEXT NOT NULL
);
"""


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


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


def capture_legacy_snapshot(connection):
    """Normalize legacy facts without changing their source semantics."""
    analysis = []
    for row in _rows(connection, "coverage_analysis"):
        file_hash, line_number, path = _legacy_key(row)
        analysis.append({
            "project_name": str(row.get("project_name") or ""),
            "file_path_hash": file_hash,
            "file_path": path,
            "line_number": line_number,
            "status": str(row.get("status") or ""),
            "is_draft": int(row.get("is_draft") or 0),
            "reviewer": str(row.get("reviewer") or ""),
            "coverage_method": str(row.get("coverage_method") or ""),
            "uncovered_reason": str(row.get("uncovered_reason") or ""),
            "comment": str(row.get("comment") or ""),
        })
    lines = []
    for row in _rows(connection, "coverage_line_index"):
        file_hash, line_number, path = _legacy_key(row)
        lines.append({
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
        })
    projects = {}
    for row in _rows(connection, "coverage_project_state"):
        projects[str(row.get("project_name") or "")] = int(row.get("data_version") or 0)
    jobs = []
    for row in _rows(connection, "coverage_background_jobs"):
        jobs.append({
            "job_id": str(row.get("job_id") or ""),
            "project_name": str(row.get("project_name") or ""),
            "kind": str(row.get("kind") or ""),
            "state": str(row.get("state") or ""),
            "data_version": int(row.get("data_version") or 0),
            "error_message": str(row.get("error_message") or ""),
        })
    return {
        "projects": sorted(name for name in _project_names(connection) if name),
        "project_data_versions": dict(sorted(projects.items())),
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
    return {
        "projects": snapshot["projects"],
        "project_data_versions": snapshot["project_data_versions"],
        "lines": snapshot["lines"],
        "analyses": snapshot["analyses"],
        "jobs": jobs,
    }


def semantic_hash(snapshot):
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def create_sqlite_schema(connection):
    connection.executescript(SQLITE_SCHEMA)
    connection.commit()


def _split_sql(sql_text):
    statements = []
    current = []
    quote = None
    for char in sql_text:
        if char in ("'", '"', chr(96)):
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
        if char == ";" and quote is None:
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def apply_schema(connection, ddl_path, release_sha=""):
    with open(ddl_path, "r", encoding="utf-8") as stream:
        ddl = stream.read()
    for statement in _split_sql(ddl):
        cursor = connection.cursor()
        cursor.execute(statement)
        cursor.close()
    existing = fetchone(connection, """
        SELECT schema_key FROM coverage_schema_meta WHERE schema_key = ?
    """, ("coverage_vnext",))
    cursor = connection.cursor()
    if existing:
        cursor.execute(
            "UPDATE coverage_schema_meta SET schema_version = ?, applied_at = ?, release_sha = ? WHERE schema_key = ?",
            (1, _now(), release_sha or "", "coverage_vnext"),
        )
    else:
        cursor.execute(
            "INSERT INTO coverage_schema_meta(schema_key, schema_version, applied_at, release_sha) VALUES (?, ?, ?, ?)",
            ("coverage_vnext", 1, _now(), release_sha or ""),
        )
    cursor.close()
    connection.commit()


def migrate_legacy(source_connection, target_connection, anomaly_path=None, release_sha=""):
    """Migrate one legacy current-state snapshot idempotently."""
    source = capture_legacy_snapshot(source_connection)
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
            scan = project_repo.create_scan(
                conn, project["id"], scan_key, "legacy_migrated", "full",
                status="ready", legacy_migrated=1,
            )
            project_repo.upsert_repository_snapshot(
                conn, scan["id"], "", verified=0, provenance="legacy_migration"
            )
            project_repo.bind_report(
                conn, scan["id"], "legacy_{}".format(scan_key[:16]),
                source_signature="legacy_migration", sidecar_schema=0,
            )
            line_rows = {
                (row["file_path_hash"], row["line_number"]): row
                for row in source["lines"] if row["project_name"] == project_name
            }
            analysis_rows = {
                (row["file_path_hash"], row["line_number"]): row
                for row in source["analyses"] if row["project_name"] == project_name
            }
            keys = sorted(set(line_rows) | set(analysis_rows))
            files = {}
            for file_hash, line_number in keys:
                source_line = line_rows.get((file_hash, line_number))
                analysis_line = analysis_rows.get((file_hash, line_number))
                path = (source_line or analysis_line or {}).get("file_path") or ""
                if not path:
                    anomalies.append({
                        "type": "missing_file_path", "project_name": project_name,
                        "file_path_hash": file_hash, "line_number": line_number,
                    })
                    path = file_hash
                if file_hash not in files:
                    file_rows = project_repo.ensure_file(
                        conn, scan["id"], "", file_hash, path, os.path.basename(path)
                    )
                    files[file_hash] = file_rows
                if source_line is None:
                    anomalies.append({
                        "type": "missing_line_index_context", "project_name": project_name,
                        "file_path_hash": file_hash, "line_number": line_number,
                    })
                    source_line = {
                        "line_number": line_number, "line_text": "",
                        "block_start_line": line_number, "block_end_line": line_number,
                        "block_type": "unknown", "function_name": "", "function_hash": "",
                        "code_line_hash": "", "code_occurrence": 1,
                    }
                line_record = dict(source_line)
                line_record["coverage_state"] = "uncovered"
                line_record["suggested_reviewer"] = ""
                line = line_repo.upsert_line(conn, files[file_hash]["id"], line_record)
                if analysis_line is not None:
                    analysis_repo.upsert(conn, line["id"], analysis_line)
            state_repo.ensure(
                conn, project["id"], current_scan_id=scan["id"], data_version=data_version
            )
            state_repo.set_current_scan(conn, project["id"], scan["id"])
            file_rows = project_repo.iter_files(conn, scan["id"])
            file_state_repo.rebuild_scan(conn, scan["id"], data_version, file_rows)
            state_repo.mark_ready(conn, project["id"], data_version)

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
                "progress": 0, "input_payload": json.dumps(old_job, sort_keys=True),
                "error_message": old_job["error_message"] if state != "interrupted"
                else "legacy active job requires manual migration decision",
                "data_version": old_job["data_version"],
            })

    result = {
        "status": "PASSED",
        "source_projects": len(source["projects"]),
        "source_line_facts": len(source["lines"]),
        "source_analysis_facts": len(source["analyses"]),
        "source_jobs": len(source["jobs"]),
        "anomalies": anomalies,
        "source_semantic_hash": semantic_hash(source),
        "target_semantic_hash": semantic_hash(
            capture_vnext_semantic_snapshot(target_connection)
        ),
        "authoritative_semantic_match": semantic_hash(source) == semantic_hash(
            capture_vnext_semantic_snapshot(target_connection)
        ),
        "release_sha": release_sha or "",
        "captured_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
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

        source = connect(load_mysql_config(args.source_config))
        target = connect(load_mysql_config(args.target_config))
        try:
            if not args.schema:
                parser.error("--schema is required for MySQL migration")
            apply_schema(target, args.schema, args.release_sha)
            first = migrate_legacy(source, target, args.anomaly_path or None, args.release_sha)
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
                    json.dump(result, stream, ensure_ascii=False, indent=2, sort_keys=True)
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if result["status"] == "PASSED" else 1
        finally:
            source.close()
            target.close()
    parser.error("provide --demo or explicit --source-config, --target-config and --schema")


if __name__ == "__main__":
    sys.exit(main())
