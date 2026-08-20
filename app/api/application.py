"""VNext API application independent from the stdlib HTTP transport."""

import os
import time

from app.api.auth import MutationAuthorizer
from app.api.router import Router
from app.api.serialization import to_jsonable


class VNextApplication(object):
    BASE = "/api/coverage"

    def __init__(self, runtime, repo_root, config):
        self.runtime = runtime
        self.config = config or {}
        self.authorizer = MutationAuthorizer(repo_root, self.config)
        self.router = Router()
        self._register_routes()

    def _register_routes(self):
        self.router.add("GET", r"^/api/coverage/health$", self.health)
        self.router.add("GET", r"^/api/coverage/release$", self.release)
        self.router.add("GET", r"^/api/coverage/projects$", self.projects)
        self.router.add("GET", r"^/api/coverage/projects/([^/]+)/scans$", self.scans)
        self.router.add("GET", r"^/api/coverage/progress$", self.progress)
        self.router.add("GET", r"^/api/coverage/incremental/unanalyzed$", self.unanalyzed)
        self.router.add("GET", r"^/api/coverage/code-layout$", self.code_layout)
        self.router.add("GET", r"^/api/coverage/code-lines$", self.code_lines)
        self.router.add("POST", r"^/api/coverage/code-lines/batch$", self.code_lines_batch)
        self.router.add("POST", r"^/api/coverage/projects$", self.create_project)
        self.router.add("POST", r"^/api/coverage/scans$", self.create_scan)
        self.router.add("POST", r"^/api/coverage/analysis$", self.save_analysis)
        self.router.add("POST", r"^/api/coverage/progress/rebuild$", self.rebuild_progress)
        self.router.add("GET", r"^/api/coverage/routes$", self.routes)
        self.router.add("GET", r"^/api/coverage/jobs$", self.jobs)
        self.router.add("POST", r"^/api/coverage/jobs/recover$", self.recover_jobs)

    def dispatch(self, method, path, query=None, body=None, headers=None, remote_address=""):
        query = query or {}
        body = body or {}
        headers = headers or {}
        try:
            return self.router.dispatch(
                method, path, query, body, headers, remote_address
            )
        except KeyError as exc:
            return 404, {"error": "not_found", "message": str(exc)}
        except ValueError as exc:
            return 400, {"error": "invalid_request", "message": str(exc)}
        except PermissionError as exc:
            message = str(exc)
            status = 403
            for candidate in (401, 403, 503):
                if message.startswith("{}:".format(candidate)):
                    status = candidate
                    message = message[len(str(candidate)) + 1:]
                    break
            return status, {"error": "forbidden", "message": message}
        except Exception as exc:
            return 500, {"error": "internal_error", "message": str(exc)}

    def _read_connection(self):
        return self.runtime.connection_context()

    def _require_mutation(self, headers, remote_address):
        allowed, status, identity = self.authorizer.authorize(headers, remote_address)
        if not allowed:
            raise PermissionError("{}:{}".format(status, identity))
        return identity

    def health(self, query, body, headers, remote_address):
        return 200, {"status": "ok", "runtime": "vnext", "schema_version": 1}

    def release(self, query, body, headers, remote_address):
        return 200, {"release": self.runtime.release_identity}

    def routes(self, query, body, headers, remote_address):
        return 200, {"base": self.BASE, "routes": self.router.describe()}

    def jobs(self, query, body, headers, remote_address):
        project_id = query.get("project_id")
        states = query.get("state")
        if isinstance(states, str):
            states = [states]
        with self._read_connection() as connection:
            return 200, {"jobs": self.runtime.jobs.list(
                connection, int(project_id) if project_id else None, states
            )}

    def recover_jobs(self, query, body, headers, remote_address):
        self._require_mutation(headers, remote_address)
        timeout = float(body.get("heartbeat_timeout", 300))
        with self._read_connection() as connection:
            from app.db.transaction import transaction
            with transaction(connection) as conn:
                recovered = self.runtime.jobs.mark_stale(conn, timeout)
        return 200, {"recovered": recovered}

    def projects(self, query, body, headers, remote_address):
        with self._read_connection() as connection:
            return 200, {"projects": self.runtime.projects.list_projects(connection)}

    def scans(self, project_name, query, body, headers, remote_address):
        with self._read_connection() as connection:
            return 200, {"project_name": project_name,
                         "scans": self.runtime.project_service.list_scans(connection, project_name)}

    def progress(self, query, body, headers, remote_address):
        project_name = str(query.get("project") or query.get("project_name") or
                           self.config.get("project_name") or "")
        if not project_name:
            raise ValueError("project is required")
        scan_id = query.get("scan_id")
        with self._read_connection() as connection:
            return 200, self.runtime.progress_service.summary(
                connection, project_name, int(scan_id) if scan_id else None
            )

    def unanalyzed(self, query, body, headers, remote_address):
        project_name = str(query.get("project") or query.get("project_name") or
                           self.config.get("project_name") or "")
        if not project_name:
            raise ValueError("project is required")
        with self._read_connection() as connection:
            return 200, {
                "project_name": project_name,
                "files": self.runtime.progress_service.pending_by_file(connection, project_name),
            }

    def _code_detail_args(self, source):
        scan_id = source.get("scan_id")
        report_id = str(source.get("report_id") or "")
        file_path = str(source.get("file_path") or "")
        if not scan_id or not report_id or not file_path:
            raise ValueError("scan_id, report_id and file_path are required")
        return int(scan_id), report_id, file_path

    def code_layout(self, query, body, headers, remote_address):
        scan_id, report_id, file_path = self._code_detail_args(query)
        with self._read_connection() as connection:
            return 200, self.runtime.code_detail.layout(
                connection, scan_id, report_id, file_path
            )

    def code_lines(self, query, body, headers, remote_address):
        scan_id, report_id, file_path = self._code_detail_args(query)
        start_line = int(query.get("start_line") or 1)
        end_line = int(query.get("end_line") or start_line)
        with self._read_connection() as connection:
            return 200, self.runtime.code_detail.lines(
                connection, scan_id, report_id, file_path, start_line, end_line
            )

    def code_lines_batch(self, query, body, headers, remote_address):
        scan_id, report_id, file_path = self._code_detail_args(body)
        ranges = body.get("ranges") or []
        if not isinstance(ranges, list) or len(ranges) > 1000:
            raise ValueError("ranges must be a list with at most 1000 entries")
        result = []
        with self._read_connection() as connection:
            for item in ranges:
                result.append(self.runtime.code_detail.lines(
                    connection, scan_id, report_id, file_path,
                    int(item.get("start_line") or 1),
                    int(item.get("end_line") or 1),
                ))
        return 200, {
            "scan_id": scan_id, "report_id": report_id,
            "file_path": file_path, "batches": result,
        }

    def create_project(self, query, body, headers, remote_address):
        self._require_mutation(headers, remote_address)
        project_name = str(body.get("project_name") or "").strip()
        if not project_name:
            raise ValueError("project_name is required")
        with self._read_connection() as connection:
            row = self.runtime.project_service.projects.ensure_project(connection, project_name)
            self.runtime.project_service.states.ensure(connection, row["id"])
            connection.commit()
            return 201, {"project": row}

    def create_scan(self, query, body, headers, remote_address):
        self._require_mutation(headers, remote_address)
        project_name = str(body.get("project_name") or "").strip()
        if not project_name:
            raise ValueError("project_name is required")
        with self._read_connection() as connection:
            if body.get("info_path"):
                result = self.runtime.scan_import_service.import_info(
                    connection, project_name, body["info_path"],
                    review_scope=body.get("review_scope", "full"),
                    repositories=body.get("repositories") or [],
                    report=body.get("report"),
                    info_file_name=body.get("info_file_name", ""),
                )
                return 201, result
            scan = self.runtime.project_service.create_scan(
                connection, project_name,
                info_file_name=body.get("info_file_name", ""),
                info_sha256=body.get("info_sha256", ""),
                review_scope=body.get("review_scope", "full"),
                repositories=body.get("repositories") or [],
                report=body.get("report"),
            )
            return 201, {"scan": scan}

    def save_analysis(self, query, body, headers, remote_address):
        identity = self._require_mutation(headers, remote_address)
        project_name = str(body.get("project_name") or "").strip()
        scan_id = body.get("scan_id")
        if not project_name or not scan_id:
            raise ValueError("project_name and scan_id are required")
        with self._read_connection() as connection:
            result = self.runtime.analysis_service.save(
                connection, project_name, int(scan_id), body.get("records") or [],
                reviewer=body.get("reviewer") or identity,
            )
        return 200, result

    def rebuild_progress(self, query, body, headers, remote_address):
        self._require_mutation(headers, remote_address)
        project_name = str(body.get("project_name") or "").strip()
        if not project_name:
            raise ValueError("project_name is required")
        with self._read_connection() as connection:
            return 200, self.runtime.progress_service.rebuild(
                connection, project_name, body.get("scan_id")
            )
