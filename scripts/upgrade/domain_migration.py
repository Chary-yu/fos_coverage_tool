"""Gate B schema/backfill transition for the canonical Analysis Domain."""

from __future__ import print_function

import hashlib
import json
import os
import re

from app.db.repositories.base import fetchone, is_sqlite, adapt_sql
from app.db.transaction import transaction
from app.services.analysis_domain_service import AnalysisDomainService
from app.time_utils import utc_sql


DOMAIN_MIGRATION_ID = "coverage-analysis-domain-v1"
DOMAIN_CONSTRAINT_MIGRATION_ID = "coverage-analysis-domain-constraints-v1"


def _constraint_file(repo_root=None):
    root = repo_root or os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    return os.path.join(root, "scripts", "upgrade", "vnext_domain_constraints.sql")


def _constraint_exists(connection, table_name, constraint_name):
    if is_sqlite(connection):
        return False
    row = fetchone(connection, """
        SELECT CONSTRAINT_NAME
        FROM information_schema.TABLE_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA=DATABASE() AND TABLE_NAME=? AND CONSTRAINT_NAME=?
    """, (table_name, constraint_name))
    return bool(row)


def _constraint_names(statement):
    """Return the table and every named constraint in one ALTER statement."""
    table_match = re.search(
        r"ALTER\s+TABLE\s+([A-Za-z0-9_]+)", statement,
        re.IGNORECASE,
    )
    names = re.findall(
        r"ADD\s+CONSTRAINT\s+([A-Za-z0-9_]+)", statement,
        re.IGNORECASE,
    )
    if not table_match or not names:
        raise ValueError("domain constraint statement lacks a named constraint")
    return table_match.group(1), names


def apply_domain_constraints(connection, release_sha="", repo_root=None):
    """Install the precise Gate B FK/index policy with a durable ledger.

    MariaDB 5.5 cannot express an idempotent ADD CONSTRAINT statement. The
    migration checks information_schema before each statement and records
    STARTED/APPLIED/FAILED independently from the core schema migration.
    SQLite keeps the fast test schema lightweight; its domain service performs
    the same cross-identity checks and the ledger still records that this
    constraint stage was evaluated.
    """
    path = _constraint_file(repo_root)
    with open(path, "r", encoding="utf-8") as stream:
        ddl = stream.read()
    ddl_sha = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    existing = fetchone(connection, """
        SELECT * FROM coverage_schema_migrations WHERE migration_id=?
    """, (DOMAIN_CONSTRAINT_MIGRATION_ID,))
    effective_release_sha = str(release_sha or "") or str(
        (existing or {}).get("release_sha") or ""
    )
    if existing and str(existing.get("state") or "") == "APPLIED":
        if str(existing.get("ddl_sha256") or "") != ddl_sha:
            raise ValueError("analysis domain constraints checksum drift")
        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, """
            UPDATE coverage_schema_meta
            SET schema_version=2, applied_at=?, release_sha=?, migration_id=?
            WHERE schema_key='coverage_analysis_domain'
        """), (utc_sql(), effective_release_sha, DOMAIN_CONSTRAINT_MIGRATION_ID))
        cursor.close()
        connection.commit()
        return {"status": "PASSED", "migration_id": DOMAIN_CONSTRAINT_MIGRATION_ID,
                "idempotent": True, "ddl_sha256": ddl_sha}

    now = utc_sql()
    cursor = connection.cursor()
    if existing:
        cursor.execute(adapt_sql(connection, """
            UPDATE coverage_schema_migrations
            SET schema_key=?, from_version=1, to_version=2, ddl_sha256=?,
                state='STARTED', started_at=?, finished_at=NULL,
                release_sha=?, error_class=''
            WHERE migration_id=?
        """), ("coverage_analysis_domain", ddl_sha, now,
                effective_release_sha, DOMAIN_CONSTRAINT_MIGRATION_ID))
    else:
        cursor.execute(adapt_sql(connection, """
            INSERT INTO coverage_schema_migrations(
                migration_id, schema_key, from_version, to_version, ddl_sha256,
                state, started_at, release_sha
            ) VALUES (?, ?, 1, 2, ?, 'STARTED', ?, ?)
        """), (DOMAIN_CONSTRAINT_MIGRATION_ID, "coverage_analysis_domain",
                ddl_sha, now, effective_release_sha))
    cursor.close()
    connection.commit()

    try:
        if not is_sqlite(connection):
            from scripts.upgrade.migration_runner import _split_sql
            for statement in _split_sql(ddl):
                table_name, constraint_names = _constraint_names(statement)
                existing_names = [
                    name for name in constraint_names
                    if _constraint_exists(connection, table_name, name)
                ]
                if existing_names and len(existing_names) != len(constraint_names):
                    raise RuntimeError(
                        "partially applied domain constraint statement on {}: {}"
                        .format(table_name, ", ".join(existing_names))
                    )
                if existing_names:
                    continue
                cursor = connection.cursor()
                cursor.execute(adapt_sql(connection, statement))
                cursor.close()

        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, """
            UPDATE coverage_schema_migrations
            SET state='APPLIED', finished_at=?, release_sha=?, error_class=''
            WHERE migration_id=?
        """), (utc_sql(), effective_release_sha, DOMAIN_CONSTRAINT_MIGRATION_ID))
        cursor.execute(adapt_sql(connection, """
            UPDATE coverage_schema_meta
            SET schema_version=2, applied_at=?, release_sha=?, migration_id=?
            WHERE schema_key='coverage_analysis_domain'
        """), (utc_sql(), effective_release_sha, DOMAIN_CONSTRAINT_MIGRATION_ID))
        cursor.close()
        connection.commit()
    except Exception as exc:
        cursor = connection.cursor()
        cursor.execute(adapt_sql(connection, """
            UPDATE coverage_schema_migrations
            SET state='FAILED', finished_at=?, release_sha=?, error_class=?
            WHERE migration_id=?
        """), (utc_sql(), effective_release_sha, type(exc).__name__,
                DOMAIN_CONSTRAINT_MIGRATION_ID))
        cursor.close()
        connection.commit()
        raise
    return {"status": "PASSED", "migration_id": DOMAIN_CONSTRAINT_MIGRATION_ID,
            "idempotent": False, "ddl_sha256": ddl_sha}


def apply_analysis_domain(connection, release_sha="", scan_id=None):
    service = AnalysisDomainService()
    with transaction(connection) as conn:
        result = service.repository.backfill_legacy(conn, scan_id=scan_id)
        audit = service.audit_consistency(conn, scan_id=scan_id)
        if audit["status"] != "PASSED":
            raise ValueError("analysis domain consistency failed: {}".format(audit))
        ddl_sha = hashlib.sha256(b"coverage-analysis-domain-v1").hexdigest()
        existing = fetchone(conn, """
            SELECT * FROM coverage_schema_migrations WHERE migration_id=?
        """, (DOMAIN_MIGRATION_ID,))
        effective_release_sha = str(release_sha or "") or str(
            (existing or {}).get("release_sha") or ""
        )
        if existing and str(existing.get("state") or "") == "APPLIED":
            if existing.get("ddl_sha256") != ddl_sha:
                raise ValueError("analysis domain migration checksum drift")
        else:
            now = utc_sql()
            cursor = conn.cursor()
            if existing:
                cursor.execute(adapt_sql(conn, """
                    UPDATE coverage_schema_migrations
                    SET state='APPLIED', to_version=1, ddl_sha256=?,
                        finished_at=?, release_sha=?, error_class=''
                    WHERE migration_id=?
                """), (ddl_sha, now, effective_release_sha, DOMAIN_MIGRATION_ID))
            else:
                cursor.execute(adapt_sql(conn, """
                    INSERT INTO coverage_schema_migrations(
                        migration_id, schema_key, from_version, to_version,
                        ddl_sha256, state, started_at, finished_at, release_sha
                    ) VALUES (?, ?, 0, 1, ?, 'APPLIED', ?, ?, ?)
                """), (DOMAIN_MIGRATION_ID, "coverage_analysis_domain", ddl_sha,
                        now, now, effective_release_sha))
            cursor.close()
        existing_meta = fetchone(conn, """
            SELECT schema_key FROM coverage_schema_meta WHERE schema_key=?
        """, ("coverage_analysis_domain",))
        cursor = conn.cursor()
        if existing_meta:
            cursor.execute(adapt_sql(conn, """
                UPDATE coverage_schema_meta SET schema_version=1, applied_at=?,
                    release_sha=?, migration_id=? WHERE schema_key=?
            """), (utc_sql(), effective_release_sha, DOMAIN_MIGRATION_ID,
                    "coverage_analysis_domain"))
        else:
            cursor.execute(adapt_sql(conn, """
                INSERT INTO coverage_schema_meta(
                    schema_key, schema_version, applied_at, release_sha, migration_id
                ) VALUES (?, 1, ?, ?, ?)
            """), ("coverage_analysis_domain", utc_sql(), effective_release_sha,
                    DOMAIN_MIGRATION_ID))
        cursor.close()
    constraints = apply_domain_constraints(
        connection, release_sha=effective_release_sha
    )
    # The v1 backfill marker is retained for compatibility, but once the
    # constraint stage has been evaluated the domain schema is at version 2.
    cursor = connection.cursor()
    cursor.execute(adapt_sql(connection, """
        UPDATE coverage_schema_meta
        SET schema_version=2, applied_at=?, release_sha=?, migration_id=?
        WHERE schema_key='coverage_analysis_domain'
    """), (utc_sql(), effective_release_sha, DOMAIN_CONSTRAINT_MIGRATION_ID))
    cursor.close()
    connection.commit()
    return {"status": "PASSED", "migration_id": DOMAIN_MIGRATION_ID,
            "backfill": result, "consistency": audit,
            "constraints": constraints}
