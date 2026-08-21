"""Gate B schema/backfill transition for the canonical Analysis Domain."""

from __future__ import print_function

import hashlib
import json

from app.db.repositories.base import fetchone, is_sqlite, adapt_sql
from app.db.transaction import transaction
from app.services.analysis_domain_service import AnalysisDomainService
from app.time_utils import utc_sql


DOMAIN_MIGRATION_ID = "coverage-analysis-domain-v1"


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
                """), (ddl_sha, now, release_sha or "", DOMAIN_MIGRATION_ID))
            else:
                cursor.execute(adapt_sql(conn, """
                    INSERT INTO coverage_schema_migrations(
                        migration_id, schema_key, from_version, to_version,
                        ddl_sha256, state, started_at, finished_at, release_sha
                    ) VALUES (?, ?, 0, 1, ?, 'APPLIED', ?, ?, ?)
                """), (DOMAIN_MIGRATION_ID, "coverage_analysis_domain", ddl_sha,
                        now, now, release_sha or ""))
            cursor.close()
        existing_meta = fetchone(conn, """
            SELECT schema_key FROM coverage_schema_meta WHERE schema_key=?
        """, ("coverage_analysis_domain",))
        cursor = conn.cursor()
        if existing_meta:
            cursor.execute(adapt_sql(conn, """
                UPDATE coverage_schema_meta SET schema_version=1, applied_at=?,
                    release_sha=?, migration_id=? WHERE schema_key=?
            """), (utc_sql(), release_sha or "", DOMAIN_MIGRATION_ID,
                    "coverage_analysis_domain"))
        else:
            cursor.execute(adapt_sql(conn, """
                INSERT INTO coverage_schema_meta(
                    schema_key, schema_version, applied_at, release_sha, migration_id
                ) VALUES (?, 1, ?, ?, ?)
            """), ("coverage_analysis_domain", utc_sql(), release_sha or "",
                    DOMAIN_MIGRATION_ID))
        cursor.close()
    return {"status": "PASSED", "migration_id": DOMAIN_MIGRATION_ID,
            "backfill": result, "consistency": audit}
