"""Current-Scan reject/undo operations with CAS and durable lineage."""

from __future__ import absolute_import

from app.db.repositories.base import adapt_sql, execute, fetchone
from app.db.repositories.project_state_repository import ProjectStateRepository
from app.db.transaction import transaction
from app.services.file_state_service import FileStateService
from app.time_utils import utc_sql


class InheritanceRejectionService(object):
    def __init__(self, state_repository=None, file_state_service=None):
        self.states = state_repository or ProjectStateRepository()
        self.file_state_service = file_state_service or FileStateService(
            state_repo=self.states
        )

    def reject(self, connection, project_id, scan_id, line_id, rejected_by,
               expected_relation_revision):
        with transaction(connection) as conn:
            self._assert_current(conn, project_id, scan_id)
            relation = fetchone(conn, """
                SELECT * FROM coverage_analysis_line_links
                WHERE scan_id=? AND line_id=? AND is_active=1
            """, (int(scan_id), int(line_id)))
            if not relation or relation.get("review_state") not in (
                    "INHERITED_PENDING", "CARRIED_COVERED"):
                raise ValueError("INHERITANCE_RELATION_NOT_ACTIVE")
            if int(relation.get("relation_revision") or 0) != int(expected_relation_revision):
                raise ValueError("STALE_RELATION_REVISION")
            now = utc_sql()
            cursor = execute(conn, """
                INSERT INTO coverage_inheritance_rejections(
                    scan_id, line_id, rejected_relation_id, rejected_relation_revision,
                    rejected_analysis_record_id, rejected_source_scan_id,
                    rejected_source_line_id, rejected_source_relation_id,
                    rejection_revision, is_active, rejected_by, rejected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
            """, (int(scan_id), int(line_id), relation["id"],
                  int(relation["relation_revision"]), relation["analysis_record_id"],
                  relation.get("source_scan_id"), relation.get("source_line_id"),
                  relation.get("source_relation_id"), rejected_by or "", now))
            cursor.close()
            cursor = execute(conn, """
                UPDATE coverage_analysis_line_links
                SET is_active=0, relation_revision=relation_revision+1,
                    updated_at=?
                WHERE id=? AND is_active=1 AND relation_revision=?
            """, (now, relation["id"], int(expected_relation_revision)))
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                cursor.close()
                raise ValueError("STALE_RELATION_REVISION")
            cursor.close()
            state = self.states.advance(conn, int(project_id))
            self.file_state_service.rebuild_validate_and_mark_ready_in_transaction(
                conn, int(project_id), int(scan_id), int(state["data_version"])
            )
            return fetchone(conn, """
                SELECT * FROM coverage_inheritance_rejections
                WHERE scan_id=? AND line_id=? ORDER BY id DESC LIMIT 1
            """, (int(scan_id), int(line_id)))

    def undo(self, connection, project_id, scan_id, line_id, rejection_id,
             expected_rejection_revision, expected_relation_revision):
        with transaction(connection) as conn:
            self._assert_current(conn, project_id, scan_id)
            rejection = fetchone(conn, """
                SELECT * FROM coverage_inheritance_rejections
                WHERE id=? AND scan_id=? AND line_id=? AND is_active=1
            """, (int(rejection_id), int(scan_id), int(line_id)))
            if not rejection:
                raise ValueError("REJECTION_NOT_ACTIVE")
            if int(rejection.get("rejection_revision") or 0) != int(expected_rejection_revision):
                raise ValueError("STALE_REJECTION_REVISION")
            relation = fetchone(conn, """
                SELECT * FROM coverage_analysis_line_links WHERE id=?
            """, (int(rejection["rejected_relation_id"]),))
            if not relation or int(relation.get("relation_revision") or 0) != int(expected_relation_revision):
                raise ValueError("STALE_RELATION_REVISION")
            now = utc_sql()
            cursor = execute(conn, """
                UPDATE coverage_analysis_line_links
                SET is_active=1, review_state='INHERITED_PENDING',
                    relation_revision=relation_revision+1, updated_at=?
                WHERE id=? AND is_active=0 AND relation_revision=?
            """, (now, relation["id"], int(expected_relation_revision)))
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                cursor.close()
                raise ValueError("STALE_RELATION_REVISION")
            cursor.close()
            cursor = execute(conn, """
                UPDATE coverage_inheritance_rejections
                SET is_active=0, terminal_reason='UNDONE', resolved_at=?,
                    rejection_revision=rejection_revision+1
                WHERE id=? AND is_active=1 AND rejection_revision=?
            """, (now, int(rejection_id), int(expected_rejection_revision)))
            if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                cursor.close()
                raise ValueError("STALE_REJECTION_REVISION")
            cursor.close()
            state = self.states.advance(conn, int(project_id))
            self.file_state_service.rebuild_validate_and_mark_ready_in_transaction(
                conn, int(project_id), int(scan_id), int(state["data_version"])
            )
            return fetchone(conn, """
                SELECT * FROM coverage_inheritance_rejections WHERE id=?
            """, (int(rejection_id),))

    def _assert_current(self, connection, project_id, scan_id):
        state = self.states.get(connection, int(project_id)) or {}
        if int(state.get("current_scan_id") or 0) != int(scan_id):
            raise ValueError("MUTATION_REQUIRES_CURRENT_SCAN")
