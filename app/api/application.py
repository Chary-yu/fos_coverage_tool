"""VNext API application independent from the stdlib HTTP transport."""

import os
import time

from app.api.auth import MutationAuthorizer
from app.api.router import Router
from app.api.serialization import to_jsonable
from app.api.endpoints import analysis as analysis_endpoint
from app.api.endpoints import code_detail as code_detail_endpoint
from app.api.endpoints import health as health_endpoint
from app.api.endpoints import incremental as incremental_endpoint
from app.api.endpoints import jobs as jobs_endpoint
from app.api.endpoints import progress as progress_endpoint
from app.api.endpoints import projects as projects_endpoint
from app.api.endpoints import release as release_endpoint


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
        self.router.add("GET", r"^/api/coverage/metrics$", self.metrics)
        self.router.add("GET", r"^/api/coverage/release$", self.release)
        self.router.add("GET", r"^/api/coverage/projects$", self.projects)
        self.router.add("GET", r"^/api/coverage/projects/([^/]+)/scans$", self.scans)
        self.router.add("GET", r"^/api/coverage/progress$", self.progress)
        self.router.add("GET", r"^/api/coverage/incremental/unanalyzed$", self.unanalyzed)
        self.router.add("POST", r"^/api/coverage/incremental$", self.incremental)
        self.router.add("GET", r"^/api/coverage/incremental$", self.incremental_result)
        self.router.add("GET", r"^/api/coverage/code-layout$", self.code_layout)
        self.router.add("GET", r"^/api/coverage/code-lines$", self.code_lines)
        self.router.add("POST", r"^/api/coverage/code-lines/batch$", self.code_lines_batch)
        self.router.add("POST", r"^/api/coverage/projects$", self.create_project)
        self.router.add("POST", r"^/api/coverage/scans$", self.create_scan)
        self.router.add("POST", r"^/api/coverage/analysis$", self.save_analysis)
        self.router.add("POST", r"^/api/coverage/progress/rebuild$", self.rebuild_progress)
        self.router.add("GET", r"^/api/coverage/routes$", self.routes)
        self.router.add("GET", r"^/api/coverage/jobs$", self.jobs)
        self.router.add("GET", r"^/api/coverage/jobs/([^/]+)$", self.job_detail)
        self.router.add("POST", r"^/api/coverage/jobs$", self.create_job)
        self.router.add("POST", r"^/api/coverage/jobs/recover$", self.recover_jobs)
        self.router.add("POST", r"^/api/coverage/exports$", self.create_export)
        self.router.add("GET", r"^/api/coverage/exports/([^/]+)$", self.export_detail)

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
        except FileNotFoundError as exc:
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
        return self.runtime.connection_context(read_only=True)

    def _write_connection(self):
        return self.runtime.connection_context(read_only=False)

    def _require_mutation(self, headers, remote_address):
        allowed, status, identity = self.authorizer.authorize(headers, remote_address)
        if not allowed:
            raise PermissionError("{}:{}".format(status, identity))
        return identity

    def health(self, query, body, headers, remote_address):
        return 200, health_endpoint.payload(self)

    def metrics(self, query, body, headers, remote_address):
        """Expose bounded runtime counters without including review data."""
        runtime = self.runtime
        payload = {
            "runtime": "vnext",
            "code_detail": runtime.code_detail.metrics(),
            "jobs": runtime.job_service.metrics(),
        }
        if runtime.database_manager is not None:
            payload["db_pool"] = runtime.database_manager.health()
        return 200, payload

    def release(self, query, body, headers, remote_address):
        return 200, release_endpoint.payload(self)

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
        recovered = self.runtime.job_service.recover(timeout)
        return 200, {"recovered": recovered}

    def job_detail(self, job_id, query, body, headers, remote_address):
        job = self.runtime.job_service.get(job_id)
        if not job:
            raise KeyError("job not found: {}".format(job_id))
        return 200, {"job": job}

    def create_job(self, query, body, headers, remote_address):
        identity = self._require_mutation(headers, remote_address)
        kind, scan_id = jobs_endpoint.request(body)
        with self._read_connection() as connection:
            scan = self.runtime.projects.get_scan(connection, int(scan_id))
            if not scan:
                raise KeyError("scan not found: {}".format(scan_id))
            project = self.runtime.projects.get_project(connection, int(scan["project_id"]))
            state = self.runtime.states.get(connection, int(scan["project_id"])) or {}
        if kind == "rebuild_progress":
            def callback():
                with self._write_connection() as callback_connection:
                    self.runtime.progress_service.rebuild(
                        callback_connection, project["project_name"], int(scan_id)
                    )
                return ""
        elif kind == "export":
            report_id = str(body.get("report_id") or "")
            output_path = body.get("output_path")

            def callback():
                with self._read_connection() as callback_connection:
                    return self.runtime.export_service.export_scan(
                        callback_connection, project["project_name"], int(scan_id),
                        report_id=report_id, output_path=output_path,
                    )
        else:
            raise ValueError("unsupported job kind: {}".format(kind))
        job = self.runtime.job_service.submit(
            project_id=project["id"], scan_id=int(scan_id), kind=kind,
            data_version=int(state.get("data_version") or 0), callback=callback,
            input_payload={"requested_by": identity, "payload": body.get("input_payload") or {}},
        )
        return 202, {"job": job}

    def create_export(self, query, body, headers, remote_address):
        values = dict(body or {})
        values["kind"] = "export"
        return self.create_job(query, values, headers, remote_address)

    def export_detail(self, job_id, query, body, headers, remote_address):
        return self.job_detail(job_id, query, body, headers, remote_address)

    def projects(self, query, body, headers, remote_address):
        with self._read_connection() as connection:
            return 200, {"projects": self.runtime.projects.list_projects(connection)}

    def scans(self, project_name, query, body, headers, remote_address):
        with self._read_connection() as connection:
            return 200, {"project_name": project_name,
                         "scans": self.runtime.project_service.list_scans(connection, project_name)}

    def progress(self, query, body, headers, remote_address):
        project_name = progress_endpoint.project_name(query, self.config.get("project_name") or "")
        scan_id = query.get("scan_id")
        with self._read_connection() as connection:
            return 200, self.runtime.progress_service.summary(
                connection, project_name, int(scan_id) if scan_id else None
            )

    def unanalyzed(self, query, body, headers, remote_address):
        project_name = progress_endpoint.project_name(query, self.config.get("project_name") or "")
        with self._read_connection() as connection:
            return 200, {
                "project_name": project_name,
                "files": self.runtime.progress_service.pending_by_file(connection, project_name),
            }

    def incremental(self, query, body, headers, remote_address):
        self._require_mutation(headers, remote_address)
        project_name = incremental_endpoint.request(body)
        with self._write_connection() as connection:
            result = self.runtime.incremental_service.build_and_persist(
                connection, project_name, int(body["scan_id"]), body["repo_path"],
                body["oldgit"], body["newgit"], body["info_path"],
                repository_name=body.get("repository_name", "default"),
                report_id=body.get("report_id", ""),
            )
        return 200, result

    def incremental_result(self, query, body, headers, remote_address):
        project_name = str(query.get("project_name") or query.get("project") or "").strip()
        scan_id = query.get("scan_id")
        repository_name = query.get("repository_name", "default")
        report_id = query.get("report_id", "")
        if not project_name or not scan_id:
            raise ValueError("project_name and scan_id are required")
        with self._read_connection() as connection:
            project = self.runtime.projects.get_project_by_name(connection, project_name)
            if not project:
                raise KeyError("project not found")
            scan = self.runtime.projects.get_scan(connection, int(scan_id))
            if not scan or int(scan["project_id"]) != int(project["id"]):
                raise KeyError("scan is not bound to project")
            result = self.runtime.incremental_results.load_payload(
                connection, int(scan_id), report_id, repository_name
            )
        if result is None:
            raise KeyError("incremental result not found")
        return 200, {"result": result}

    def _code_detail_args(self, source):
        return code_detail_endpoint.identity(source)

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
        if not ranges:
            return 200, {
                "scan_id": scan_id, "report_id": report_id,
                "file_path": file_path, "batches": [],
            }
        with self._read_connection() as connection:
            result = self.runtime.code_detail.lines_batch(
                connection, scan_id, report_id, file_path, ranges
            )
        return 200, {
            "scan_id": scan_id, "report_id": report_id,
            "file_path": file_path, "batches": result,
        }

    def create_project(self, query, body, headers, remote_address):
        self._require_mutation(headers, remote_address)
        project_name = projects_endpoint.project_name(body)
        with self._write_connection() as connection:
            row = self.runtime.project_service.ensure_project(connection, project_name)
            return 201, {"project": row}

    def create_scan(self, query, body, headers, remote_address):
        self._require_mutation(headers, remote_address)
        project_name = projects_endpoint.project_name(body)
        with self._write_connection() as connection:
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
        project_name, scan_id, records = analysis_endpoint.request(body)
        with self._write_connection() as connection:
            result = self.runtime.analysis_service.save(
                connection, project_name, scan_id, records,
                reviewer=body.get("reviewer") or identity,
            )
        return 200, result

    def rebuild_progress(self, query, body, headers, remote_address):
        self._require_mutation(headers, remote_address)
        project_name = str(body.get("project_name") or "").strip()
        if not project_name:
            raise ValueError("project_name is required")
        with self._write_connection() as connection:
            return 200, self.runtime.progress_service.rebuild(
                connection, project_name, body.get("scan_id")
            )
