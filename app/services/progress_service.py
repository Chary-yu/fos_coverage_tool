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
            aggregate = self.file_states.scan_aggregate(connection, scan_id)
            if aggregate and int(aggregate.get("file_count") or 0) > 0:
                return self._aggregate_file_state_summary(
                    project_name, scan_id, state, aggregate
                )
        result = self.file_states.scan_summary_from_facts(connection, scan_id)
        result.update({
            "project_name": project_name,
            "source": "authoritative",
            "data_version": int(state.get("data_version") or 0),
            "file_state_version": int(state.get("file_state_version") or 0),
        })
        return result

    @staticmethod
    def _aggregate_file_state_summary(project_name, scan_id, state, aggregate):
        return {
            "project_name": project_name, "scan_id": scan_id,
            "source": "coverage_file_state",
            "data_version": int(state.get("data_version") or 0),
            "file_state_version": int(state.get("file_state_version") or 0),
            "file_count": int(aggregate.get("file_count") or 0),
            "total_lines": int(aggregate.get("total_lines") or 0),
            "total_uncovered": int(aggregate.get("total_uncovered") or 0),
            "filled_total": int(aggregate.get("filled_total") or 0),
            "draft_total": int(aggregate.get("draft_total") or 0),
            "confirmed_total": int(aggregate.get("confirmed_total") or 0),
            "pending_total": int(aggregate.get("pending_total") or 0),
            # Pending references are intentionally served by the separate
            # /incremental/unanalyzed endpoint so the progress homepage stays
            # an O(1) aggregate query.
            "pending_line_references": [],
        }

    def rebuild(self, connection, project_name, scan_id=None):
        project = self._project(connection, project_name)
        state = self.states.get(connection, project["id"]) or {}
        scan_id = scan_id or state.get("current_scan_id")
        if not scan_id:
            return self.summary(connection, project_name, scan_id)
        with transaction(connection) as conn:
            self.file_states.rebuild_scan(
                conn, scan_id, int(state.get("data_version") or 0), None
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
            "scan_id": int(scan_id),
            "repository_name": key[0], "file_path": key[1],
            "pending_line_numbers": values,
            "unanalyzed": len(values),
        } for key, values in grouped.items()]

    def pending_page(self, connection, project_name, scan_id=None,
                     page=1, page_size=100):
        project = self._project(connection, project_name)
        state = self.states.get(connection, project["id"]) or {}
        scan_id = scan_id or state.get("current_scan_id")
        page = max(1, int(page))
        page_size = min(500, max(1, int(page_size)))
        if not scan_id:
            return {"scan_id": None, "page": page, "page_size": page_size,
                    "total": 0, "total_pages": 0, "rows": []}
        offset = (page - 1) * page_size
        rows = self.file_states.pending_line_references(
            connection, int(scan_id), limit=page_size, offset=offset
        )
        total = self.file_states.pending_line_count(connection, int(scan_id))
        return {
            "scan_id": int(scan_id), "page": page, "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
            "rows": rows,
        }
