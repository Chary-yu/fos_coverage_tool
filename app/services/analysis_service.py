"""Analysis write/read service with one authoritative transaction boundary."""

from app.db.repositories import (
    AnalysisRepository,
    ProjectRepository,
    ProjectStateRepository,
)
from app.db.transaction import transaction


CONFIRMED_STATUSES = ("可覆盖", "无法覆盖", "冗余代码")
VALID_STATUSES = ("", "未确认") + CONFIRMED_STATUSES


class AnalysisService(object):
    def __init__(self, analysis_repo=None, project_repo=None, state_repo=None):
        self.analyses = analysis_repo or AnalysisRepository()
        self.projects = project_repo or ProjectRepository()
        self.states = state_repo or ProjectStateRepository()

    def _project(self, connection, project_name=None, project_id=None):
        if project_id is not None:
            row = self.projects.get_project(connection, project_id)
        else:
            row = self.projects.get_project_by_name(connection, project_name)
        if not row:
            raise KeyError("project not found")
        return row

    def save(self, connection, project_name, scan_id, records, reviewer=""):
        project = self._project(connection, project_name=project_name)
        scan = self.projects.get_scan(connection, int(scan_id))
        if not scan or int(scan.get("project_id")) != int(project["id"]):
            raise KeyError("scan is not bound to project")
        records = list(records or [])
        with transaction(connection) as conn:
            state = self.states.ensure(conn, project["id"], current_scan_id=scan_id)
            if not records:
                self.states.set_current_scan(conn, project["id"], scan_id)
                return {"saved": 0, "data_version": state.get("data_version", 0)}
            saved = 0
            for item in records:
                line_id = item.get("line_id")
                if not line_id:
                    file_row = self.projects.get_file(
                        conn, int(scan_id), item.get("repository_name", ""),
                        item.get("file_path_hash", ""),
                    )
                    if not file_row:
                        raise KeyError("file identity not found")
                    line = self._line_by_number(conn, file_row["id"], item.get("line_number"))
                    if not line:
                        raise KeyError("physical source line not found")
                    line_id = line["id"]
                else:
                    line = self._line_identity(conn, line_id)
                    if not line or int(line.get("scan_id")) != int(scan_id):
                        raise KeyError("physical source line is not in scan")
                values = dict(item)
                if reviewer and not values.get("reviewer"):
                    values["reviewer"] = reviewer
                status = values.get("status") or ""
                if status not in VALID_STATUSES:
                    raise ValueError("invalid analysis status: {}".format(status))
                self.analyses.upsert(conn, int(line_id), values)
                saved += 1
            next_state = self.states.advance(conn, project["id"])
            self.states.set_current_scan(conn, project["id"], scan_id)
        return {"saved": saved, "data_version": next_state["data_version"]}

    @staticmethod
    def _line_by_number(connection, file_id, line_number):
        from app.db.repositories.base import fetchone
        return fetchone(connection, """
            SELECT * FROM coverage_lines WHERE file_id = ? AND line_number = ?
        """, (file_id, int(line_number)))

    @staticmethod
    def _line_identity(connection, line_id):
        from app.db.repositories.base import fetchone
        return fetchone(connection, """
            SELECT l.*, f.scan_id, f.repository_name, f.file_path, f.file_path_hash
            FROM coverage_lines l JOIN coverage_files f ON f.id = l.file_id
            WHERE l.id = ?
        """, (int(line_id),))

    def read_for_file(self, connection, file_id):
        return self.analyses.get_by_file(connection, file_id)

    def read_for_scan(self, connection, scan_id):
        return self.analyses.get_by_scan(connection, scan_id)

    def effective_reviewer(self, connection, line_id):
        from app.db.repositories.base import fetchone
        line = fetchone(connection, "SELECT suggested_reviewer FROM coverage_lines WHERE id = ?",
                        (int(line_id),))
        analysis = self.analyses.get_by_line(connection, int(line_id))
        saved = str((analysis or {}).get("reviewer") or "").strip()
        suggested = str((line or {}).get("suggested_reviewer") or "").strip()
        return saved or suggested or ""
