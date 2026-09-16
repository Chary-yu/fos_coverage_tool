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
        scope = " AND q.scan_id=?" if scan_id is not None else ""
        scope_params = (int(scan_id),) if scan_id is not None else ()
        orphan_links = fetchone(connection, """
            SELECT COUNT(*) AS total
            FROM coverage_analysis_line_links q
            LEFT JOIN coverage_analysis_records r ON r.id=q.analysis_record_id
            LEFT JOIN coverage_lines l ON l.id=q.line_id
            LEFT JOIN coverage_files f ON f.id=l.file_id
            WHERE 1=1 {scope}
              AND (r.id IS NULL OR l.id IS NULL OR f.id IS NULL
                   OR f.scan_id<>q.scan_id)
        """.format(scope=scope), scope_params)
        record_references = fetchone(connection, """
            SELECT
                COALESCE(SUM(CASE WHEN q.analysis_record_id IS NULL AND
                                      b.originating_record_id IS NULL AND
                                      x.rejected_analysis_record_id IS NULL
                                  THEN 1 ELSE 0 END), 0)
                    AS truly_unreferenced,
                COALESCE(SUM(CASE WHEN q.analysis_record_id IS NULL AND
                                      (b.originating_record_id IS NOT NULL OR
                                       x.rejected_analysis_record_id IS NOT NULL)
                                  THEN 1 ELSE 0 END), 0)
                    AS historical_referenced,
                COALESCE(SUM(CASE WHEN q.analysis_record_id IS NULL THEN 1 ELSE 0 END), 0)
                    AS not_linked_to_line
            FROM coverage_analysis_records r
            LEFT JOIN (
                SELECT DISTINCT analysis_record_id
                FROM coverage_analysis_line_links
            ) q ON q.analysis_record_id=r.id
            LEFT JOIN (
                SELECT DISTINCT originating_record_id
                FROM coverage_analysis_blocks
                WHERE originating_record_id IS NOT NULL
            ) b ON b.originating_record_id=r.id
            LEFT JOIN (
                SELECT DISTINCT rejected_analysis_record_id
                FROM coverage_inheritance_rejections
            ) x ON x.rejected_analysis_record_id=r.id
        """) or {}
        cross_scan_blocks = fetchone(connection, """
            SELECT COUNT(*) AS total
            FROM coverage_analysis_blocks b
            LEFT JOIN coverage_files f ON f.id=b.file_id
            WHERE f.id IS NULL OR b.scan_id<>f.scan_id
        """)
        duplicate_links = fetchone(connection, """
            SELECT COUNT(*) AS total FROM (
                SELECT scan_id, line_id, COUNT(*) AS c
                FROM coverage_analysis_line_links
                GROUP BY scan_id, line_id HAVING COUNT(*)>1
            ) duplicates
        """)
        link_identity = fetchone(connection, """
            SELECT COUNT(*) AS total
            FROM coverage_analysis_line_links q
            LEFT JOIN coverage_lines l ON l.id=q.line_id
            LEFT JOIN coverage_files f ON f.id=l.file_id
            LEFT JOIN coverage_analysis_blocks b ON b.id=q.analysis_block_id
            LEFT JOIN coverage_inheritance_groups g ON g.id=q.inheritance_group_id
            WHERE 1=1 {scope}
              AND (
                    l.id IS NULL OR f.id IS NULL OR f.scan_id<>q.scan_id
                    OR (b.id IS NOT NULL AND
                        (b.scan_id<>q.scan_id OR b.file_id<>l.file_id OR
                         b.start_line<1 OR b.end_line<b.start_line OR
                         l.line_number<b.start_line OR l.line_number>b.end_line))
                    OR (q.inheritance_group_id IS NOT NULL AND
                        (g.id IS NULL OR g.candidate_scan_id<>q.scan_id OR
                         g.candidate_file_id<>l.file_id OR
                         g.source_analysis_block_id IS NULL))
                  )
        """.format(scope=scope), scope_params)
        invalid_review_states = fetchone(connection, """
            SELECT COUNT(*) AS total
            FROM coverage_analysis_line_links q
            WHERE 1=1 {scope}
              AND q.review_state NOT IN
                  ('MANUAL_DRAFT','MANUAL_CONFIRMED',
                   'INHERITED_PENDING','CARRIED_COVERED')
        """.format(scope=scope), scope_params)
        # A manual link has no source triple.  An inheritance link must have
        # a complete, internally consistent source triple.
        source_identity = fetchone(connection, """
            SELECT COUNT(*) AS total
            FROM coverage_analysis_line_links q
            LEFT JOIN coverage_analysis_line_links sr
              ON sr.id=q.source_relation_id
            WHERE 1=1 {scope}
              AND (
                    (q.relation_origin='INHERITANCE' AND (
                        q.source_scan_id IS NULL OR q.source_line_id IS NULL
                        OR q.source_relation_id IS NULL OR sr.id IS NULL
                        OR sr.scan_id<>q.source_scan_id
                        OR sr.line_id<>q.source_line_id
                    ))
                    OR (q.relation_origin<>'INHERITANCE' AND (
                        q.source_scan_id IS NOT NULL OR q.source_line_id IS NOT NULL
                        OR q.source_relation_id IS NOT NULL
                    ))
                  )
        """.format(scope=scope), scope_params)
        rejection_identity = fetchone(connection, """
            SELECT COUNT(*) AS total
            FROM coverage_inheritance_rejections x
            LEFT JOIN coverage_analysis_line_links q
              ON q.id=x.rejected_relation_id
            WHERE x.is_active=1
              AND (q.id IS NULL OR q.scan_id<>x.scan_id OR q.line_id<>x.line_id
                   OR q.is_active<>0
                   OR q.relation_revision<>x.rejected_relation_revision+1
                   OR q.analysis_record_id<>x.rejected_analysis_record_id
                   OR COALESCE(q.source_scan_id, 0)<>COALESCE(x.rejected_source_scan_id, 0)
                   OR COALESCE(q.source_line_id, 0)<>COALESCE(x.rejected_source_line_id, 0)
                   OR COALESCE(q.source_relation_id, 0)<>COALESCE(x.rejected_source_relation_id, 0))
        """)
        duplicate_rejections = fetchone(connection, """
            SELECT COUNT(*) AS total FROM (
                SELECT scan_id, line_id, COUNT(*) AS c
                FROM coverage_inheritance_rejections
                WHERE is_active=1 GROUP BY scan_id, line_id HAVING COUNT(*)>1
            ) duplicates
        """)
        counts = {
            "orphan_links": int((orphan_links or {}).get("total") or 0),
            # ``orphan_records`` is retained as the compatibility alias for
            # the genuinely unreferenced subset. Historical Block/rejection
            # references are valid audit graph edges even without a current
            # LineLink.
            "orphan_records": int(record_references.get("truly_unreferenced") or 0),
            "not_linked_to_current_line": int(
                record_references.get("not_linked_to_line") or 0
            ),
            "historical_referenced_records": int(
                record_references.get("historical_referenced") or 0
            ),
            "cross_scan_blocks": int((cross_scan_blocks or {}).get("total") or 0),
            "duplicate_current_links": int((duplicate_links or {}).get("total") or 0),
            "link_identity_mismatches": int((link_identity or {}).get("total") or 0),
            "invalid_review_states": int((invalid_review_states or {}).get("total") or 0),
            "source_identity_mismatches": int((source_identity or {}).get("total") or 0),
            "rejection_identity_mismatches": int((rejection_identity or {}).get("total") or 0),
            "duplicate_active_rejections": int((duplicate_rejections or {}).get("total") or 0),
        }
        diagnostic_only = {
            "not_linked_to_current_line", "historical_referenced_records",
        }
        global_blocking = {
            key: value for key, value in counts.items()
            if key not in diagnostic_only
        }
        # Historical graph hygiene is reported globally, but a candidate
        # consistency gate must not fail because of an unrelated truly
        # unreferenced record that cannot affect this Scan. Link/block/review
        # identity checks remain scoped by the candidate scan above.
        if scan_id is not None:
            global_blocking.pop("orphan_records", None)
        global_status = "PASSED" if not any(counts.values()) else "FAILED"
        return {
            "status": "PASSED" if not any(global_blocking.values()) else "FAILED",
            "global_status": global_status,
            "checks": counts,
            "scan_id": scan_id,
        }

    def read_line(self, connection, scan_id, line_id):
        return self.repository.read_line(connection, scan_id, line_id)

    def read_scan(self, connection, scan_id):
        return self.repository.read_scan(connection, scan_id)
