"""The only runtime owner allowed to change project CURRENT."""

from __future__ import absolute_import

from app.db.repositories.base import execute, fetchone
from app.db.repositories.project_state_repository import ProjectStateRepository
from app.db.transaction import transaction


_UNSET = object()


class ScanPublicationService(object):
    def __init__(self, state_repository=None):
        self.states = state_repository or ProjectStateRepository()

    def publish(self, connection, project_id, scan_id,
                expected_current_scan_id=_UNSET, read_set=None, fence=None):
        with transaction(connection) as conn:
            return self.publish_in_transaction(
                conn, project_id, scan_id,
                expected_current_scan_id=expected_current_scan_id,
                read_set=read_set, fence=fence,
            )

    def publish_in_transaction(self, connection, project_id, scan_id,
                               expected_current_scan_id=_UNSET, read_set=None,
                               fence=None):
        scan = fetchone(connection, "SELECT * FROM coverage_scans WHERE id=?",
                        (int(scan_id),))
        if not scan or int(scan.get("project_id")) != int(project_id):
            raise KeyError("candidate scan is not bound to project")
        if str(scan.get("status") or "").upper() not in ("SEALED", "READY"):
            raise ValueError("candidate scan is not sealed")
        state = self.states.ensure(connection, project_id)
        current = state.get("current_scan_id")
        if expected_current_scan_id is not _UNSET and (
                (current is None and expected_current_scan_id is not None) or
                (current is not None and expected_current_scan_id is None) or
                (current is not None and expected_current_scan_id is not None and
                 int(current) != int(expected_current_scan_id))):
            raise ValueError("CURRENT_POINTER_CHANGED")
        predecessor = scan.get("predecessor_scan_id")
        if expected_current_scan_id is not _UNSET and (
                (predecessor is None and expected_current_scan_id is not None) or
                (predecessor is not None and expected_current_scan_id is None) or
                (predecessor is not None and expected_current_scan_id is not None and
                 int(predecessor) != int(expected_current_scan_id))):
            raise ValueError("PREDECESSOR_MISMATCH")
        if read_set:
            self._validate_read_set(connection, read_set)
        if fence:
            fence()
        if expected_current_scan_id is _UNSET:
            update_sql = """
                UPDATE coverage_project_state
                SET current_scan_id=?, updated_at=CURRENT_TIMESTAMP
                WHERE project_id=?
            """
            update_params = (int(scan_id), int(project_id))
        elif expected_current_scan_id is None:
            update_sql = """
                UPDATE coverage_project_state
                SET current_scan_id=?, updated_at=CURRENT_TIMESTAMP
                WHERE project_id=? AND current_scan_id IS NULL
            """
            update_params = (int(scan_id), int(project_id))
        else:
            # The expected CURRENT value is part of the write predicate.  The
            # earlier read is useful for diagnostics, but cannot be the CAS
            # itself because another publisher may commit between the read
            # and this UPDATE.
            update_sql = """
                UPDATE coverage_project_state
                SET current_scan_id=?, updated_at=CURRENT_TIMESTAMP
                WHERE project_id=? AND current_scan_id=?
            """
            update_params = (int(scan_id), int(project_id),
                             int(expected_current_scan_id))
        cursor = execute(connection, update_sql, update_params)
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            cursor.close()
            raise ValueError("CURRENT_POINTER_CHANGED")
        cursor.close()
        return self.states.get(connection, project_id)

    @staticmethod
    def _validate_read_set(connection, read_set):
        for item in read_set or []:
            if not isinstance(item, dict):
                raise ValueError("READ_SET_CHANGED")
            if item.get("relation_id") is not None:
                row = fetchone(connection, """
                    SELECT relation_revision FROM coverage_analysis_line_links WHERE id=?
                """, (int(item["relation_id"]),))
                if (item.get("relation_revision") is None or not row or
                        int(row.get("relation_revision") or 0) !=
                        int(item.get("relation_revision"))):
                    raise ValueError("READ_SET_CHANGED")
            if item.get("record_id") is not None:
                row = fetchone(connection, """
                    SELECT content_revision FROM coverage_analysis_records WHERE id=?
                """, (int(item["record_id"]),))
                if (item.get("content_revision") is None or not row or
                        int(row.get("content_revision") or 0) !=
                        int(item.get("content_revision"))):
                    raise ValueError("READ_SET_CHANGED")
            if item.get("rejection_id") is not None:
                row = fetchone(connection, """
                    SELECT rejection_revision, is_active
                    FROM coverage_inheritance_rejections WHERE id=?
                """, (int(item["rejection_id"]),))
                if (not row or
                        int(row.get("rejection_revision") or 0) !=
                        int(item.get("rejection_revision") or 0) or
                        (item.get("rejection_is_active") is not None and
                         int(row.get("is_active") or 0) !=
                         int(item.get("rejection_is_active") or 0))):
                    raise ValueError("READ_SET_CHANGED")
