"""Freshness-aware progress service over authoritative VNext facts."""

from app.db.repositories import FileStateRepository, ProjectRepository, ProjectStateRepository
from app.db.transaction import transaction


class ProgressService(object):
    def __init__(self, file_state_repo=None, project_repo=None, state_repo=None):
        self.file_states = file_state_repo or FileStateRepository()
        self.projects = project_repo or ProjectRepository()
        self.states = state_repo or ProjectStateRepository()

    def _project(self, connection, project_name):
        project = self.projects.get_project_by_name(connection, project_name)
        if not project:
            raise KeyError("project not found: {}".format(project_name))
        return project

    def summary(self, connection, project_name, scan_id=None):
        project = self._project(connection, project_name)
        state = self.states.get(connection, project["id"]) or {
            "data_version": 0, "file_state_version": 0
        }
        if scan_id is None:
            scan_id = state.get("current_scan_id")
        if not scan_id:
            return {
                "project_name": project_name, "scan_id": None, "source": "authoritative",
                "data_version": int(state.get("data_version") or 0),
                "file_state_version": int(state.get("file_state_version") or 0),
                "total_uncovered": 0, "filled_total": 0, "draft_total": 0,
                "confirmed_total": 0, "pending_total": 0, "pending_line_references": [],
            }
        ready = (
            int(state.get("file_state_version") or 0) == int(state.get("data_version") or 0)
            and int(state.get("data_version") or 0) > 0
        )
        if ready:
            rows = self._file_state_rows(connection, scan_id)
            if rows:
                return self._aggregate_file_states(connection, project_name, scan_id, state, rows)
        result = self.file_states.scan_summary_from_facts(connection, scan_id)
        result.update({
            "project_name": project_name,
            "source": "authoritative",
            "data_version": int(state.get("data_version") or 0),
            "file_state_version": int(state.get("file_state_version") or 0),
        })
        return result

    @staticmethod
    def _file_state_rows(connection, scan_id):
        from app.db.repositories.base import fetchall
        return fetchall(connection, """
            SELECT * FROM coverage_file_state WHERE scan_id = ? ORDER BY file_id
        """, (scan_id,))

    def _aggregate_file_states(self, connection, project_name, scan_id, state, rows):
        pending = self.file_states.pending_line_references(connection, scan_id)
        return {
            "project_name": project_name, "scan_id": scan_id,
            "source": "coverage_file_state",
            "data_version": int(state.get("data_version") or 0),
            "file_state_version": int(state.get("file_state_version") or 0),
            "total_uncovered": sum(int(row.get("total_uncovered") or 0) for row in rows),
            "filled_total": sum(int(row.get("filled_total") or 0) for row in rows),
            "draft_total": sum(int(row.get("draft_total") or 0) for row in rows),
            "confirmed_total": sum(int(row.get("confirmed_total") or 0) for row in rows),
            "pending_total": sum(int(row.get("pending_total") or 0) for row in rows),
            "pending_line_references": [
                "{}:{}:{}".format(
                    item["repository_name"], item["file_path"], item["line_number"]
                ) for item in pending
            ],
        }

    def rebuild(self, connection, project_name, scan_id=None):
        project = self._project(connection, project_name)
        state = self.states.get(connection, project["id"]) or {}
        scan_id = scan_id or state.get("current_scan_id")
        if not scan_id:
            return self.summary(connection, project_name, scan_id)
        with transaction(connection) as conn:
            file_rows = self.projects.iter_files(conn, scan_id)
            self.file_states.rebuild_scan(
                conn, scan_id, int(state.get("data_version") or 0), file_rows
            )
            self.states.mark_ready(conn, project["id"], int(state.get("data_version") or 0))
        return self.summary(connection, project_name, scan_id)

    def pending_by_file(self, connection, project_name, scan_id=None):
        project = self._project(connection, project_name)
        state = self.states.get(connection, project["id"]) or {}
        scan_id = scan_id or state.get("current_scan_id")
        if not scan_id:
            return []
        from app.db.repositories.base import fetchall
        rows = self.file_states.pending_line_references(connection, scan_id)
        grouped = {}
        for row in rows:
            key = (row.get("repository_name") or "", row.get("file_path") or "")
            grouped.setdefault(key, []).append(int(row["line_number"]))
        return [{
            "repository_name": key[0], "file_path": key[1],
            "pending_line_numbers": values,
        } for key, values in grouped.items()]
