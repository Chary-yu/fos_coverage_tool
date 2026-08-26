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

    def _resolve_scan_for_project(self, connection, project, state,
                                  scan_id=None, require_current=False):
        """Resolve a Scan only after binding it to the requested Project."""
        resolved = int(scan_id or state.get("current_scan_id") or 0)
        if not resolved:
            return None
        scan = self.projects.get_scan(connection, resolved)
        if not scan:
            raise KeyError("scan not found")
        if int(scan.get("project_id") or 0) != int(project["id"]):
            raise ValueError("INVALID_SCAN_IDENTITY")
        if (require_current and
                resolved != int(state.get("current_scan_id") or 0)):
            raise ValueError("MUTATION_REQUIRES_CURRENT_SCAN")
        return resolved

    def summary(self, connection, project_name, scan_id=None):
        project = self._project(connection, project_name)
        state = self.states.get(connection, project["id"]) or {
            "data_version": 0, "file_state_version": 0
        }
        scan_id = self._resolve_scan_for_project(
            connection, project, state, scan_id=scan_id,
        )
        if not scan_id:
            return {
                "project_name": project_name, "scan_id": None, "source": "authoritative",
                "data_version": int(state.get("data_version") or 0),
                "file_state_version": int(state.get("file_state_version") or 0),
                "total_uncovered": 0, "filled_total": 0, "draft_total": 0,
                "confirmed_total": 0, "pending_total": 0,
                "ordinary_pending_total": 0, "inherited_pending_total": 0,
                "manual_draft_pending_total": 0, "pending_line_references": [],
                "pending_conservation": {"status": "PASSED", "mismatched_files": 0},
            }
        ready = (
            int(state.get("file_state_version") or 0) == int(state.get("data_version") or 0)
            and int(state.get("data_version") or 0) > 0
        )
        if ready:
            aggregate = self.file_states.scan_aggregate(connection, scan_id)
            if aggregate and int(aggregate.get("file_count") or 0) > 0:
                result = self._aggregate_file_state_summary(
                    project_name, scan_id, state, aggregate
                )
                result["pending_conservation"] = self.file_states.pending_conservation(
                    connection, int(scan_id)
                )
                return result
        result = self.file_states.scan_summary_from_facts(connection, scan_id)
        result.update({
            "project_name": project_name,
            "source": "authoritative",
            "data_version": int(state.get("data_version") or 0),
            "file_state_version": int(state.get("file_state_version") or 0),
            "pending_conservation": {
                "status": "PASSED" if int(result.get("pending_total") or 0) == (
                    int(result.get("ordinary_pending_total") or 0)
                    + int(result.get("inherited_pending_total") or 0)
                    + int(result.get("manual_draft_pending_total") or 0)
                ) else "FAILED",
                "mismatched_files": 0,
            },
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
            "ordinary_pending_total": int(aggregate.get("ordinary_pending_total") or 0),
            "inherited_pending_total": int(aggregate.get("inherited_pending_total") or 0),
            "manual_draft_pending_total": int(aggregate.get("manual_draft_pending_total") or 0),
            # Pending references are intentionally served by the separate
            # /incremental/unanalyzed endpoint so the progress homepage stays
            # an O(1) aggregate query.
            "pending_line_references": [],
        }

    def rebuild(self, connection, project_name, scan_id=None):
        project = self._project(connection, project_name)
        state = self.states.get(connection, project["id"]) or {}
        scan_id = self._resolve_scan_for_project(
            connection, project, state, scan_id=scan_id, require_current=True,
        )
        if not scan_id:
            return self.summary(connection, project_name, scan_id)
        with transaction(connection) as conn:
            self.file_states.rebuild_scan(
                conn, scan_id, int(state.get("data_version") or 0), None
            )
            self.states.mark_ready(conn, project["id"], int(state.get("data_version") or 0))
        return self.summary(connection, project_name, scan_id)

    def pending_by_file(self, connection, project_name, scan_id=None,
                        page_size=200, cursor=None):
        project = self._project(connection, project_name)
        state = self.states.get(connection, project["id"]) or {}
        scan_id = self._resolve_scan_for_project(
            connection, project, state, scan_id=scan_id,
        )
        if not scan_id:
            return {"rows": [], "has_more": False, "next_cursor": None}
        page = self.file_states.pending_file_page(
            connection, int(scan_id), limit=page_size, cursor=cursor
        )
        return page

    def files_page(self, connection, project_name, scan_id=None,
                   page_size=200, cursor=None):
        project = self._project(connection, project_name)
        state = self.states.get(connection, project["id"]) or {}
        scan_id = self._resolve_scan_for_project(
            connection, project, state, scan_id=scan_id,
        )
        if not scan_id:
            return {"rows": [], "has_more": False, "next_cursor": None}
        return self.file_states.file_page(
            connection, int(scan_id), limit=page_size, cursor=cursor,
            pending_only=False,
        )

    def pending_page(self, connection, project_name, scan_id=None,
                     page_size=100, cursor=None):
        project = self._project(connection, project_name)
        state = self.states.get(connection, project["id"]) or {}
        scan_id = self._resolve_scan_for_project(
            connection, project, state, scan_id=scan_id,
        )
        page_size = min(500, max(1, int(page_size)))
        if not scan_id:
            return {"scan_id": None, "page_size": page_size, "total": 0,
                    "rows": [], "has_more": False, "next_cursor": None}
        rows = self.file_states.pending_line_references(
            connection, int(scan_id), limit=page_size + 1, cursor=cursor
        )
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = {
                "file_id": int(last["file_id"]),
                "line_number": int(last["line_number"]),
                "line_id": int(last["line_id"]),
            }
        total = None
        if (int(state.get("data_version") or 0) > 0 and
                int(state.get("file_state_version") or 0) == int(
                    state.get("data_version") or 0
                )):
            aggregate = self.file_states.scan_aggregate(connection, int(scan_id))
            if aggregate:
                total = int(aggregate.get("pending_total") or 0)
        if total is None:
            # A stale/missing derived state is already an exceptional path;
            # keep the list itself keyset-paginated and use one authoritative
            # fallback rather than COUNT(*) on every page.
            total = int(
                (self.file_states.scan_summary_from_facts(
                    connection, int(scan_id)
                ) or {}).get("pending_total") or 0
            )
        return {
            "scan_id": int(scan_id), "page_size": page_size, "total": total,
            "rows": rows, "has_more": has_more, "next_cursor": next_cursor,
        }

    def detail_page(self, connection, scan_id, file_id, page_size=200,
                    cursor=None):
        return self.file_states.line_detail_page(
            connection, int(scan_id), int(file_id), limit=page_size,
            cursor=cursor,
        )
