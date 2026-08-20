"""Analysis write/read service with one authoritative transaction boundary."""

from app.db.repositories import (
    AnalysisRepository,
    LineIndexRepository,
    ProjectRepository,
    ProjectStateRepository,
)
from app.db.transaction import transaction


CONFIRMED_STATUSES = ("可覆盖", "无法覆盖", "冗余代码")
VALID_STATUSES = ("", "未确认") + CONFIRMED_STATUSES


class AnalysisService(object):
    def __init__(self, analysis_repo=None, project_repo=None, state_repo=None, line_repo=None):
        self.analyses = analysis_repo or AnalysisRepository()
        self.projects = project_repo or ProjectRepository()
        self.states = state_repo or ProjectStateRepository()
        self.lines = line_repo or LineIndexRepository()

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
            resolved = self._resolve_records(conn, int(scan_id), records, reviewer)
            self.analyses.upsert_many(conn, resolved)
            saved = len(resolved)
            next_state = self.states.advance(conn, project["id"])
            self.states.set_current_scan(conn, project["id"], scan_id)
        return {"saved": saved, "data_version": next_state["data_version"]}

    def _resolve_records(self, connection, scan_id, records, reviewer):
        """Resolve all line identities before issuing the single bulk write."""
        resolved = [dict(item or {}) for item in records]
        direct_ids = {int(item["line_id"]) for item in resolved if item.get("line_id")}
        identity_requests = {
            (str(item.get("repository_name") or ""), str(item.get("file_path_hash") or ""))
            for item in resolved if not item.get("line_id")
        }
        direct_rows = {
            int(row["id"]): row for row in self.lines.get_by_ids(connection, direct_ids)
        }
        file_rows = self.projects.get_files_by_identities(
            connection, scan_id, identity_requests
        )
        pair_requests = set()
        for item in resolved:
            if item.get("line_id"):
                continue
            identity = (
                str(item.get("repository_name") or ""),
                str(item.get("file_path_hash") or ""),
            )
            file_row = file_rows.get(identity)
            if not file_row:
                raise KeyError("file identity not found")
            pair_requests.add((int(file_row["id"]), int(item.get("line_number") or 0)))
        number_rows = {
            (int(row["file_id"]), int(row["line_number"])): row
            for row in self.lines.get_by_file_numbers(connection, pair_requests)
        }

        for item in resolved:
            if item.get("line_id"):
                line_id = int(item["line_id"])
                line = direct_rows.get(line_id)
                if not line or int(line.get("scan_id")) != int(scan_id):
                    raise KeyError("physical source line is not in scan")
            else:
                identity = (
                    str(item.get("repository_name") or ""),
                    str(item.get("file_path_hash") or ""),
                )
                file_row = file_rows[identity]
                line = number_rows.get((int(file_row["id"]), int(item.get("line_number") or 0)))
                if not line:
                    raise KeyError("physical source line not found")
                line_id = int(line["id"])
            item["line_id"] = line_id
            if reviewer and not item.get("reviewer"):
                item["reviewer"] = reviewer
            status = item.get("status") or ""
            if status not in VALID_STATUSES:
                raise ValueError("invalid analysis status: {}".format(status))
        return resolved

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
