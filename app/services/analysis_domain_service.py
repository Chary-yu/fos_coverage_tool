"""Business-level checks for the canonical Analysis Domain."""

from __future__ import absolute_import

from app.db.repositories.analysis_domain_repository import AnalysisDomainRepository
from app.db.repositories.base import fetchall, fetchone
from app.db.transaction import transaction


class AnalysisDomainService(object):
    def __init__(self, repository=None):
        self.repository = repository or AnalysisDomainRepository()

    def backfill(self, connection, scan_id=None):
        with transaction(connection) as conn:
            return self.repository.backfill_legacy(conn, scan_id=scan_id)

    def audit_consistency(self, connection, scan_id=None):
        clauses = []
        params = []
        if scan_id is not None:
            clauses.append("q.scan_id=?")
            params.append(int(scan_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        orphan_links = fetchone(connection, """
            SELECT COUNT(*) AS total
            FROM coverage_analysis_line_links q
            LEFT JOIN coverage_analysis_records r ON r.id=q.analysis_record_id
            LEFT JOIN coverage_lines l ON l.id=q.line_id
            JOIN coverage_files f ON f.id=l.file_id
            {where} AND (r.id IS NULL OR f.scan_id<>q.scan_id)
        """.format(where=where or " WHERE 1=1"), params)
        orphan_records = fetchone(connection, """
            SELECT COUNT(*) AS total
            FROM coverage_analysis_records r
            LEFT JOIN coverage_analysis_line_links q ON q.analysis_record_id=r.id
            WHERE q.id IS NULL
        """)
        cross_scan_blocks = fetchone(connection, """
            SELECT COUNT(*) AS total
            FROM coverage_analysis_blocks b
            JOIN coverage_files f ON f.id=b.file_id
            WHERE b.scan_id<>f.scan_id
        """)
        duplicate_links = fetchone(connection, """
            SELECT COUNT(*) AS total FROM (
                SELECT scan_id, line_id, COUNT(*) AS c
                FROM coverage_analysis_line_links
                GROUP BY scan_id, line_id HAVING COUNT(*)>1
            ) duplicates
        """)
        counts = {
            "orphan_links": int((orphan_links or {}).get("total") or 0),
            "orphan_records": int((orphan_records or {}).get("total") or 0),
            "cross_scan_blocks": int((cross_scan_blocks or {}).get("total") or 0),
            "duplicate_current_links": int((duplicate_links or {}).get("total") or 0),
        }
        return {"status": "PASSED" if not any(counts.values()) else "FAILED",
                "checks": counts, "scan_id": scan_id}

    def read_line(self, connection, scan_id, line_id):
        return self.repository.read_line(connection, scan_id, line_id)

    def read_scan(self, connection, scan_id):
        return self.repository.read_scan(connection, scan_id)
