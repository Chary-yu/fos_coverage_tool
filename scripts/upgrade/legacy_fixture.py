"""Reusable Legacy schema fixture builder for migration rehearsals.

The production migration must tolerate optional columns, so this fixture is
deliberately richer than the minimal historical unit-test schema.  It uses
the same four source tables as the inventory contract and can be populated
with deterministic business facts for SQLite or a disposable MariaDB.
"""

from __future__ import print_function

import hashlib
import os
import sqlite3

from app.db.repositories.base import adapt_sql, is_sqlite
from app.time_utils import utc_sql


FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "tests", "fixtures", "legacy_schema_mariadb55.sql",
)


def _split_sql(sql_text):
    statements = []
    current = []
    for part in (sql_text or "").split(";"):
        lines = []
        for line in part.splitlines():
            if line.strip().startswith("--"):
                continue
            lines.append(line)
        statement = "\n".join(lines).strip()
        if statement:
            statements.append(statement)
    return statements


def create_legacy_fixture_schema(connection, fixture_path=None):
    """Create the inventory-compatible Legacy tables on a test connection."""
    path = fixture_path or FIXTURE_PATH
    with open(path, "r", encoding="utf-8") as stream:
        ddl = stream.read()
    for statement in _split_sql(ddl):
        if is_sqlite(connection):
            statement = statement.replace("BIGINT NOT NULL AUTO_INCREMENT", "INTEGER")
            statement = statement.replace(" ENGINE=InnoDB DEFAULT CHARSET=utf8mb4", "")
        cursor = connection.cursor()
        try:
            cursor.execute(adapt_sql(connection, statement))
        except Exception as exc:
            compact = " ".join(str(statement).split())
            raise RuntimeError(
                "legacy fixture DDL statement failed: {}".format(compact[:2000])
            ) from exc
        finally:
            cursor.close()
    connection.commit()


def _executemany_batched(connection, statement, rows, batch_size=1000):
    """Keep fixture loading bounded on both SQLite and MariaDB."""
    rows = list(rows or [])
    for start in range(0, len(rows), int(batch_size)):
        cursor = connection.cursor()
        try:
            cursor.executemany(
                adapt_sql(connection, statement),
                rows[start:start + int(batch_size)],
            )
        finally:
            cursor.close()


def seed_legacy_fixture(connection, project_name="fixture", line_count=2,
                        analysis_count=None, job_count=0):
    """Populate deterministic facts and return source counts.

    ``line_count`` and ``analysis_count`` are intentionally independent so
    analysis-only rows can be exercised by migration tests.
    """
    analysis_count = line_count if analysis_count is None else int(analysis_count)
    stamp = utc_sql()
    path = "src/fixture.c"
    path_hash = hashlib.md5(path.encode("utf-8")).hexdigest()
    _executemany_batched(connection, """
        INSERT INTO coverage_line_index(
            project_name, file_path, file_path_hash, source_file_name,
            line_number, line_text, block_start_line, block_end_line,
            block_type, function_name, function_hash, code_line_hash,
            code_occurrence, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (project_name, path, path_hash, "fixture.c", line_number,
         "line {};".format(line_number), line_number, line_number,
         "single", "main", "fn", "line-{}".format(line_number), 1,
         stamp, stamp)
        for line_number in range(1, int(line_count) + 1)
    ])
    _executemany_batched(connection, """
        INSERT INTO coverage_analysis(
            project_name, file_path, file_path_hash, source_file_name,
            line_number, reviewer, status, is_draft, coverage_method,
            uncovered_reason, comment, remark, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (project_name, path, path_hash, "fixture.c", line_number,
         "reviewer", "可覆盖", 0, "unit", "", "comment", "remark",
         stamp, stamp)
        for line_number in range(1, int(analysis_count) + 1)
    ])
    cursor = connection.cursor()
    cursor.execute(adapt_sql(connection, """
        INSERT INTO coverage_project_state(
            project_name, data_version, file_state_version, current_scan_key,
            updated_at
        ) VALUES (?, ?, ?, ?, ?)
    """), (project_name, 7, 7, "legacy-key", stamp))
    cursor.close()
    _executemany_batched(connection, """
        INSERT INTO coverage_background_jobs(
            job_id, project_name, kind, state, percent, progress_unit,
            stage, message, input_payload, result_path, filename, row_count,
            data_version, heartbeat_at, finished_at, error_message,
            created_at, started_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        ("job-{}".format(index), project_name, "export", "completed",
         100, "percent", "done", "ok", "{\"input\":true}",
         "/legacy/result.zip", "result.zip", 2, 7, stamp, stamp, "",
         stamp, stamp, stamp)
        for index in range(int(job_count))
    ])
    connection.commit()
    return {"projects": 1, "lines": int(line_count),
            "analyses": int(analysis_count), "jobs": int(job_count)}


def sqlite_legacy_fixture(line_count=2, analysis_count=None, job_count=0):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    create_legacy_fixture_schema(connection)
    seed_legacy_fixture(connection, line_count=line_count,
                        analysis_count=analysis_count, job_count=job_count)
    return connection
