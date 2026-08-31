"""Canonical current-Scan inheritance review mutations.

The HTTP application owns authentication and request shaping.  This service
owns the review state transition boundary so confirm/edit/reject/undo cannot
silently grow a second business implementation in the transport layer.
"""

from __future__ import absolute_import

from app.db.repositories.base import adapt_sql, execute, fetchone
from app.db.repositories.project_state_repository import ProjectStateRepository
from app.db.transaction import transaction
from app.inheritance.rejections import InheritanceRejectionService
from app.services.file_state_service import FileStateService
from app.time_utils import utc_sql


class InheritanceReviewService(object):
    def __init__(self, state_repository=None, analysis_service=None,
                 rejection_service=None, file_state_service=None):
        self.states = state_repository or ProjectStateRepository()
        self.analysis_service = analysis_service
        self.file_state_service = file_state_service or FileStateService(
            state_repo=self.states
        )
        self.rejections = rejection_service or InheritanceRejectionService(
            self.states, self.file_state_service
        )

    def confirm(self, connection, project_id, scan_id, selected_line_ids,
                expected_relation_revisions=None, default_expected_revision=None,
                reviewer=""):
        """Confirm selected active inherited relations atomically."""
        selected = [int(item) for item in (selected_line_ids or []) if item]
        if not selected:
            raise ValueError("line_id and expected_relation_revision are required")
        if len(selected) > 500:
            raise ValueError("selected_line_ids exceeds limit")
        expected_map = expected_relation_revisions or {}
        with transaction(connection) as conn:
            self._assert_current(conn, project_id, scan_id)
            affected_file_ids = self.file_state_service.file_states.file_ids_for_lines(
                conn, int(scan_id), selected
            )
            results = []
            for line_id in selected:
                expected = int(expected_map.get(str(line_id), expected_map.get(
                    line_id, default_expected_revision or 0
                )) or 0)
                if not expected:
                    raise ValueError("line_id and expected_relation_revision are required")
                relation = fetchone(conn, """
                    SELECT * FROM coverage_analysis_line_links
                    WHERE scan_id=? AND line_id=? AND is_active=1
                """, (int(scan_id), line_id))
                if not relation or int(relation.get("relation_revision") or 0) != expected:
                    raise ValueError("STALE_RELATION_REVISION")
                if relation.get("review_state") not in (
                        "INHERITED_PENDING", "CARRIED_COVERED"):
                    raise ValueError("INHERITANCE_RELATION_NOT_ACTIVE")
                now = utc_sql()
                cursor = execute(conn, adapt_sql(conn,
                    "UPDATE coverage_analysis_line_links "
                    "SET review_state='MANUAL_CONFIRMED', reviewed_by=?, "
                    "reviewed_at=?, relation_revision=relation_revision+1, "
                    "updated_at=? WHERE id=? AND relation_revision=? AND is_active=1"),
                    (reviewer or "", now, now, relation["id"], expected))
                if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                    cursor.close()
                    raise ValueError("STALE_RELATION_REVISION")
                cursor.close()
                results.append({
                    "line_id": line_id,
                    "review_state": "MANUAL_CONFIRMED",
                    "relation_revision": expected + 1,
                    "reviewed_by": reviewer or "",
                })
            state = self.states.advance(conn, int(project_id))
            self.file_state_service.rebuild_validate_and_mark_ready_in_transaction(
                conn, int(project_id), int(scan_id), int(state["data_version"]),
                affected_file_ids=affected_file_ids,
            )
        return {"scan_id": int(scan_id), "items": results,
                "reviewed_by": reviewer or ""}

    def edit_confirm(self, connection, project_name, scan_id, records,
                     reviewer=""):
        """Persist a manual edit through AnalysisService's CAS transaction."""
        if self.analysis_service is None:
            raise RuntimeError("analysis service is not configured")
        return self.analysis_service.save(
            connection, project_name, int(scan_id), records or [],
            reviewer=reviewer or "", enforce_current=True,
        )

    def reject(self, connection, project_id, scan_id, line_id, rejected_by,
               expected_relation_revision):
        return self.rejections.reject(
            connection, project_id, scan_id, line_id, rejected_by,
            expected_relation_revision,
        )

    def undo(self, connection, project_id, scan_id, line_id, rejection_id,
             expected_rejection_revision, expected_relation_revision):
        return self.rejections.undo(
            connection, project_id, scan_id, line_id, rejection_id,
            expected_rejection_revision, expected_relation_revision,
        )

    def _assert_current(self, connection, project_id, scan_id):
        state = self.states.get(connection, int(project_id)) or {}
        if int(state.get("current_scan_id") or 0) != int(scan_id):
            raise ValueError("MUTATION_REQUIRES_CURRENT_SCAN")
