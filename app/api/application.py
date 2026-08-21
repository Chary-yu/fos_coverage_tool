"""VNext API application independent from the stdlib HTTP transport."""

import os
import base64
import json
import time
import logging
import sys

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on some platforms
    resource = None

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
from app.code_detail.source_reader import compute_db_file_path_hash
from app.db.repositories.base import adapt_sql, fetchall, fetchone
from app.inheritance.rejections import InheritanceRejectionService
from app.scan_import import RepositoryBusyError
from app.db.transaction import transaction
from app.time_utils import utc_sql


logger = logging.getLogger(__name__)


def _process_peak_rss_bytes():
    """Return the process high-water RSS in bytes when the OS exposes it."""
    if resource is None:
        return 0
    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss or 0)
    except (AttributeError, OSError, TypeError, ValueError):
        return 0
    # Linux and most Unix implementations report KiB; macOS reports bytes.
    if sys.platform == "darwin":
        return max(0, value)
    return max(0, value * 1024)


class VNextApplication(object):
    BASE = "/api/coverage"

    def __init__(self, runtime, repo_root, config):
        self.runtime = runtime
        self.config = config or {}
        self.authorizer = MutationAuthorizer(repo_root, self.config)
        self.rejections = InheritanceRejectionService(runtime.states)
        self.router = Router()
        self._register_routes()

    def _register_routes(self):
        self.router.add("GET", r"^/api/coverage/health$", self.health)
        self.router.add("GET", r"^/api/coverage/metrics$", self.metrics)
        self.router.add("GET", r"^/api/coverage/release$", self.release)
        self.router.add("GET", r"^/api/coverage/projects$", self.projects)
        self.router.add("GET", r"^/api/coverage/projects/([^/]+)/scans$", self.scans)
        self.router.add("GET", r"^/api/coverage/scans/([^/]+)/inheritance/pending$", self.inheritance_pending)
        self.router.add("GET", r"^/api/coverage/scans/([^/]+)/inheritance/decisions$", self.inheritance_decisions)
        self.router.add("POST", r"^/api/coverage/scans/([^/]+)/inheritance/confirm$", self.inheritance_confirm)
        self.router.add("POST", r"^/api/coverage/scans/([^/]+)/inheritance/edit-confirm$", self.inheritance_edit_confirm)
        self.router.add("POST", r"^/api/coverage/scans/([^/]+)/inheritance/reject$", self.inheritance_reject)
        self.router.add("POST", r"^/api/coverage/scans/([^/]+)/inheritance/rejections/([^/]+)/undo$", self.inheritance_undo_rejection)
        self.router.add("POST", r"^/api/coverage/scans/([^/]+)/inheritance/undo$", self.inheritance_undo)
        self.router.add("GET", r"^/api/coverage/progress$", self.progress)
        self.router.add("GET", r"^/api/coverage/progress/details$", self.progress_details)
        self.router.add("GET", r"^/api/coverage/progress/pending$", self.progress_pending)
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
        self.router.add("GET", r"^/api/coverage/exports/([^/]+)/download$", self.export_download)
        self.router.add("GET", r"^/api/coverage/exports/([^/]+)$", self.export_detail)

    def dispatch(self, method, path, query=None, body=None, headers=None, remote_address=""):
        query = query or {}
        body = body or {}
        headers = headers or {}
        try:
            if method.upper() == "GET" and self._read_auth_required(path):
                self._require_read(headers, remote_address)
            return self.router.dispatch(
                method, path, query, body, headers, remote_address
            )
        except KeyError:
            return 404, {"error": "not_found", "message": "resource not found"}
        except FileNotFoundError:
            return 404, {"error": "not_found", "message": "resource not found"}
        except RepositoryBusyError:
            return 409, {"error": "REPOSITORY_BUSY", "message": "repository is busy"}
        except ValueError as exc:
            raw_code = str(exc or "").split(":", 1)[0].strip()
            error_map = {
                "MUTATION_REQUIRES_CURRENT_SCAN": ("SCAN_NOT_CURRENT_FOR_MUTATION", 409),
                "STALE_RELATION_REVISION": ("STALE_RELATION_REVISION", 409),
                "STALE_CONTENT_REVISION": ("STALE_RECORD_REVISION", 409),
                "EXPECTED_RECORD_REVISION_REQUIRED": ("STALE_RECORD_REVISION", 409),
                "STALE_REJECTION_REVISION": ("STALE_REJECTION_REVISION", 409),
                "CURRENT_POINTER_CHANGED": ("CURRENT_POINTER_CHANGED", 409),
                "PREDECESSOR_MISMATCH": ("INVALID_SCAN_IDENTITY", 409),
                "READ_SET_CHANGED": ("STALE_RECORD_REVISION", 409),
                "REJECTION_NOT_ACTIVE": ("UNDO_NOT_ALLOWED", 409),
                "INHERITANCE_RELATION_NOT_ACTIVE": ("UNDO_NOT_ALLOWED", 409),
                "PAGINATION_CURSOR_STALE": ("PAGINATION_CURSOR_STALE", 409),
            }
            code, status = error_map.get(raw_code, ("invalid_request", 400))
            return status, {"error": code, "message": "request was rejected"}
        except PermissionError as exc:
            message = "request is not authorized"
            raw_message = str(exc)
            status = 403
            for candidate in (401, 403, 503):
                if raw_message.startswith("{}:".format(candidate)):
                    status = candidate
                    break
            return status, {"error": "forbidden", "message": message}
        except Exception as exc:
            logger.exception("VNext application dispatch failed")
            return 500, {"error": "internal_error", "message": "internal server error"}

    def _read_auth_required(self, path):
        """Require operator auth for reads on an explicitly public bind."""
        host = str((self.config.get("server") or {}).get("host") or "127.0.0.1").lower()
        loopback_hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
        if host in loopback_hosts:
            return False
        # Health and release are intentionally public for process probes; all
        # data and operational reads on a non-loopback bind are protected.
        return path not in ("/api/coverage/health", "/api/coverage/release")

    def _require_read(self, headers, remote_address):
        if str((self.config.get("auth") or {}).get("mode") or "reverse_proxy").lower() == "disabled":
            raise PermissionError("401:non-loopback reads require operator authentication")
        return self._require_operator(headers, remote_address)

    def _read_connection(self):
        return self.runtime.connection_context(read_only=True)

    def _write_connection(self):
        return self.runtime.connection_context(read_only=False)

    def _require_mutation(self, headers, remote_address):
        allowed, status, identity = self.authorizer.authorize_mutation(headers, remote_address)
        if not allowed:
            raise PermissionError("{}:{}".format(status, identity))
        return identity

    def _require_role(self, headers, remote_address, roles):
        allowed, status, identity = self.authorizer.authorize_role(
            headers, remote_address, roles
        )
        if not allowed:
            raise PermissionError("{}:role_not_permitted".format(status))
        return identity

    def _require_operator(self, headers, remote_address):
        allowed, status, identity = self.authorizer.authenticate_operator(headers, remote_address)
        if not allowed:
            raise PermissionError("{}:{}".format(status, identity))
        return identity

    def health(self, query, body, headers, remote_address):
        return 200, health_endpoint.payload(self)

    def metrics(self, query, body, headers, remote_address):
        """Expose bounded runtime counters without including review data."""
        self._require_operator(headers, remote_address)
        runtime = self.runtime
        payload = {
            "runtime": "vnext",
            "code_detail": runtime.code_detail.metrics(),
            "jobs": runtime.job_service.metrics(),
            "process": {
                "peak_rss_bytes": _process_peak_rss_bytes(),
            },
        }
        if runtime.database_manager is not None:
            payload["db_pool"] = runtime.database_manager.health()
        return 200, payload

    def release(self, query, body, headers, remote_address):
        return 200, release_endpoint.payload(self)

    def routes(self, query, body, headers, remote_address):
        self._require_operator(headers, remote_address)
        return 200, {"base": self.BASE, "routes": self.router.describe()}

    def jobs(self, query, body, headers, remote_address):
        self._require_operator(headers, remote_address)
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
        self._require_operator(headers, remote_address)
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

    def export_download(self, job_id, query, body, headers, remote_address):
        self._require_operator(headers, remote_address)
        job = self.runtime.job_service.get(job_id)
        if not job or job.get("kind") not in ("export", ""):
            raise KeyError("export job not found")
        if job.get("state") != "completed":
            raise ValueError("export is not completed")
        path = self.runtime.export_service.download_path(job.get("result_path"), job_id)
        return 200, {
            "__download__": path,
            "filename": os.path.basename(path),
            "content_type": "application/zip",
        }

    def projects(self, query, body, headers, remote_address):
        with self._read_connection() as connection:
            return 200, {"projects": self.runtime.projects.list_projects(connection)}

    def scans(self, project_name, query, body, headers, remote_address):
        with self._read_connection() as connection:
            return 200, {"project_name": project_name,
                         "scans": self.runtime.project_service.list_scans(connection, project_name)}

    @staticmethod
    def _decode_cursor(value, scan_id, data_version, filter_key):
        if not value:
            return None
        try:
            raw = base64.urlsafe_b64decode(str(value).encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict) or "id" not in payload:
                raise ValueError
            if (int(payload.get("scan_id")) != int(scan_id) or
                    int(payload.get("data_version")) != int(data_version) or
                    str(payload.get("filter") or "") != str(filter_key)):
                raise ValueError("stale")
            return int(payload["id"])
        except Exception:
            # A cursor is bound to the exact Scan snapshot and filter.  Never
            # silently reuse an old id against a new data version.
            raise ValueError("PAGINATION_CURSOR_STALE")

    @staticmethod
    def _encode_cursor(identifier, scan_id, data_version, filter_key):
        if identifier is None:
            return None
        raw = json.dumps({
            "id": int(identifier), "scan_id": int(scan_id),
            "data_version": int(data_version), "filter": str(filter_key),
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii")

    def _inheritance_scan(self, connection, scan_id):
        scan = self.runtime.projects.get_scan(connection, int(scan_id))
        if not scan:
            raise KeyError("scan not found")
        project = self.runtime.projects.get_project(connection, int(scan["project_id"]))
        if not project:
            raise KeyError("project not found")
        return scan, project

    def inheritance_pending(self, scan_id, query, body, headers, remote_address):
        limit = min(500, max(1, int(query.get("limit") or 100)))
        filter_key = "pending"
        with self._read_connection() as connection:
            _, project = self._inheritance_scan(connection, int(scan_id))
            state = self.runtime.states.get(connection, int(project["id"])) or {}
            data_version = int(state.get("data_version") or 0)
            cursor = self._decode_cursor(
                query.get("cursor"), scan_id, data_version, filter_key
            )
            clauses = [
                "d.candidate_scan_id=?", "d.decision='INHERITED'",
                "q.is_active=1", "q.review_state='INHERITED_PENDING'",
            ]
            params = [int(scan_id)]
            if cursor is not None:
                clauses.append("d.id>?" )
                params.append(cursor)
            rows = fetchall(connection, """
                SELECT d.id AS decision_id, d.candidate_line_id, d.reason_code,
                       d.algorithm_version, d.evaluated_at, l.line_number,
                       f.file_path, f.repository_name, q.review_state,
                       q.relation_revision, q.analysis_record_id,
                       r.conclusion_status, r.coverage_method, r.uncovered_reason,
                       r.comment
                FROM coverage_inheritance_decisions d
                JOIN coverage_lines l ON l.id=d.candidate_line_id
                JOIN coverage_files f ON f.id=l.file_id
                LEFT JOIN coverage_analysis_line_links q
                  ON q.scan_id=d.candidate_scan_id AND q.line_id=d.candidate_line_id
                 AND q.is_active=1
                LEFT JOIN coverage_analysis_records r ON r.id=q.analysis_record_id
                WHERE {where}
                ORDER BY d.id LIMIT ?
            """.format(where=" AND ".join(clauses)), params + [limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
        return 200, {
            "scan_id": int(scan_id), "limit": limit,
            "data_version": data_version,
            "items": rows, "next_cursor": self._encode_cursor(
                rows[-1]["decision_id"] if has_more and rows else None,
                scan_id, data_version, filter_key,
            ), "has_more": has_more,
        }

    def inheritance_decisions(self, scan_id, query, body, headers, remote_address):
        limit = min(500, max(1, int(query.get("limit") or 100)))
        reason_code = str(query.get("reason_code") or "").strip()
        filter_key = "decisions:" + reason_code
        with self._read_connection() as connection:
            _, project = self._inheritance_scan(connection, int(scan_id))
            state = self.runtime.states.get(connection, int(project["id"])) or {}
            data_version = int(state.get("data_version") or 0)
            cursor = self._decode_cursor(
                query.get("cursor"), scan_id, data_version, filter_key
            )
            clauses = ["candidate_scan_id=?"]
            params = [int(scan_id)]
            if reason_code:
                clauses.append("reason_code=?")
                params.append(reason_code)
            if cursor is not None:
                clauses.append("id>?" )
                params.append(cursor)
            rows = fetchall(connection, """
                SELECT * FROM coverage_inheritance_decisions
                WHERE {where} ORDER BY id LIMIT ?
            """.format(where=" AND ".join(clauses)), params + [limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
        return 200, {"scan_id": int(scan_id), "data_version": data_version,
                     "reason_code": reason_code, "items": rows,
                     "next_cursor": self._encode_cursor(
                         rows[-1]["id"] if has_more and rows else None,
                         scan_id, data_version, filter_key,
                     ),
                     "has_more": has_more}

    def _current_scan_context(self, connection, scan_id):
        scan, project = self._inheritance_scan(connection, scan_id)
        state = self.runtime.states.get(connection, int(project["id"])) or {}
        if int(state.get("current_scan_id") or 0) != int(scan_id):
            raise ValueError("MUTATION_REQUIRES_CURRENT_SCAN")
        return scan, project

    def inheritance_confirm(self, scan_id, query, body, headers, remote_address):
        identity = self._require_role(headers, remote_address, ("reviewer", "admin"))
        values = body or {}
        selected = values.get("selected_line_ids") or [values.get("line_id")]
        selected = [int(item) for item in selected if item]
        expected_map = values.get("expected_relation_revisions") or {}
        if not selected:
            raise ValueError("line_id and expected_relation_revision are required")
        if len(selected) > 500:
            raise ValueError("selected_line_ids exceeds limit")
        with self._write_connection() as connection:
            with transaction(connection) as conn:
                scan, project = self._current_scan_context(conn, scan_id)
                results = []
                for line_id in selected:
                    expected = int(expected_map.get(str(line_id),
                                      expected_map.get(line_id,
                                      values.get("expected_relation_revision") or 0)) or 0)
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
                    cursor = conn.cursor()
                    cursor.execute(adapt_sql(conn,
                        "UPDATE coverage_analysis_line_links SET review_state='MANUAL_CONFIRMED', "
                        "reviewed_by=?, reviewed_at=?, relation_revision=relation_revision+1, "
                        "updated_at=? WHERE id=? AND relation_revision=? AND is_active=1"),
                        (identity, utc_sql(), utc_sql(), relation["id"], expected))
                    if int(getattr(cursor, "rowcount", 0) or 0) != 1:
                        cursor.close()
                        raise ValueError("STALE_RELATION_REVISION")
                    cursor.close()
                    results.append({"line_id": line_id, "review_state": "MANUAL_CONFIRMED"})
                self.runtime.states.advance(conn, int(project["id"]))
            return 200, {"scan_id": int(scan_id), "items": results,
                         "reviewed_by": identity}

    def inheritance_edit_confirm(self, scan_id, query, body, headers, remote_address):
        identity = self._require_role(headers, remote_address, ("reviewer", "admin"))
        with self._write_connection() as connection:
            scan, project = self._current_scan_context(connection, scan_id)
            records = (body or {}).get("records") or []
            if not records:
                raise ValueError("records are required")
            expected_revision = (body or {}).get("expected_record_revision")
            expected_relation = (body or {}).get("expected_relation_revision")
            expected_relation_map = (body or {}).get("expected_relation_revisions") or {}
            if expected_revision is None and any(
                    item.get("expected_record_revision") is None
                    for item in records if isinstance(item, dict)):
                raise ValueError("EXPECTED_RECORD_REVISION_REQUIRED")
            if expected_relation is None and not expected_relation_map and any(
                    item.get("expected_relation_revision") is None
                    for item in records if isinstance(item, dict)):
                raise ValueError("STALE_RELATION_REVISION")
            if expected_revision is not None:
                records = [
                    dict(item, expected_record_revision=item.get(
                        "expected_record_revision", expected_revision
                    ))
                    for item in records
                ]
            if expected_relation is not None or expected_relation_map:
                records = [
                    dict(item, expected_relation_revision=item.get(
                        "expected_relation_revision",
                        expected_relation_map.get(str(item.get("line_id")),
                                                  expected_relation_map.get(
                                                      item.get("line_id"), expected_relation)),
                    ))
                    for item in records
                ]
            # The client-side reviewer field is a UI convenience/suggestion,
            # never an identity credential. Persist the authenticated
            # operator that performed the mutation for every selected line.
            records = [dict(item, reviewer=identity) for item in records]
            result = self.runtime.analysis_service.save(
                connection, project["project_name"], int(scan_id), records,
                reviewer=identity,
                enforce_current=True,
            )
        return 200, result

    def inheritance_reject(self, scan_id, query, body, headers, remote_address):
        identity = self._require_role(headers, remote_address, ("reviewer", "admin"))
        line_id = int((body or {}).get("line_id") or 0)
        expected = int((body or {}).get("expected_relation_revision") or 0)
        with self._write_connection() as connection:
            scan, project = self._current_scan_context(connection, scan_id)
            rejection = self.rejections.reject(
                connection, project["id"], int(scan_id), line_id, identity, expected
            )
        return 200, {"rejection": rejection}

    def inheritance_undo(self, scan_id, query, body, headers, remote_address):
        identity = self._require_role(headers, remote_address, ("reviewer", "admin"))
        del identity
        with self._write_connection() as connection:
            scan, project = self._current_scan_context(connection, scan_id)
            rejection = self.rejections.undo(
                connection, project["id"], int(scan_id), int((body or {}).get("line_id") or 0),
                int((body or {}).get("rejection_id") or 0),
                int((body or {}).get("expected_rejection_revision") or 0),
                int((body or {}).get("expected_relation_revision") or 0),
            )
        return 200, {"rejection": rejection}

    def inheritance_undo_rejection(self, scan_id, rejection_id, query, body,
                                   headers, remote_address):
        values = dict(body or {})
        values["rejection_id"] = int(rejection_id)
        return self.inheritance_undo(
            scan_id, query, values, headers, remote_address
        )

    def progress(self, query, body, headers, remote_address):
        project_name = progress_endpoint.project_name(query, self.config.get("project_name") or "")
        scan_id = query.get("scan_id")
        with self._read_connection() as connection:
            return 200, self.runtime.progress_service.summary(
                connection, project_name, int(scan_id) if scan_id else None
            )

    def progress_details(self, query, body, headers, remote_address):
        project_name = progress_endpoint.project_name(query, self.config.get("project_name") or "")
        scan_id = query.get("scan_id")
        file_path = str(query.get("file") or query.get("file_path") or "").strip()
        repository_name = str(query.get("repository_name") or "").strip()
        if not scan_id or not file_path:
            raise ValueError("scan_id and file are required")
        page = max(1, int(query.get("page") or 1))
        page_size = min(200, max(1, int(query.get("page_size") or 200)))
        project = None
        with self._read_connection() as connection:
            project = self.runtime.projects.get_project_by_name(connection, project_name)
            if not project:
                raise KeyError("project not found")
            scan = self.runtime.projects.get_scan(connection, int(scan_id))
            if not scan or int(scan["project_id"]) != int(project["id"]):
                raise KeyError("scan is not bound to project")
            file_hash = compute_db_file_path_hash(file_path, repository_name)
            file_rows = fetchall(connection, """
                SELECT * FROM coverage_files
                WHERE scan_id = ? AND repository_name = ?
                  AND (file_path_hash = ? OR file_path = ?)
            """, (int(scan_id), repository_name, file_hash, file_path))
            if len(file_rows) > 1:
                raise ValueError("file path is ambiguous within the Scan identity")
            file_row = file_rows[0] if file_rows else None
            if not file_row:
                raise KeyError("file identity not found")
            total_row = fetchone(connection, """
                SELECT COUNT(*) AS total FROM coverage_lines WHERE file_id = ?
            """, (int(file_row["id"]),))
            offset = (page - 1) * page_size
            rows = fetchall(connection, """
                SELECT l.line_number, l.line_text, l.coverage_state,
                       l.suggested_reviewer,
                       CASE WHEN x.id IS NOT NULL THEN ''
                            WHEN q.id IS NOT NULL THEN r.conclusion_status
                            ELSE a.status END AS status,
                       CASE WHEN x.id IS NOT NULL THEN 1
                            WHEN q.id IS NOT NULL THEN
                                CASE WHEN q.review_state IN ('MANUAL_DRAFT', 'INHERITED_PENDING')
                                     THEN 1 ELSE 0 END
                            ELSE a.is_draft END AS is_draft,
                       CASE WHEN x.id IS NOT NULL THEN ''
                            WHEN q.id IS NOT NULL THEN q.reviewed_by
                            ELSE a.reviewer END AS reviewer,
                       CASE WHEN x.id IS NOT NULL THEN ''
                            WHEN q.id IS NOT NULL THEN r.coverage_method
                            ELSE a.coverage_method END AS coverage_method,
                       CASE WHEN x.id IS NOT NULL THEN ''
                            WHEN q.id IS NOT NULL THEN r.uncovered_reason
                            ELSE a.uncovered_reason END AS uncovered_reason,
                       CASE WHEN x.id IS NOT NULL THEN x.rejected_at
                            WHEN q.id IS NOT NULL THEN r.updated_at
                            ELSE a.updated_at END AS updated_at,
                       CASE WHEN x.id IS NOT NULL THEN 'INHERITANCE_REJECTED'
                            ELSE q.review_state END AS review_state,
                       q.relation_origin, q.analysis_record_id, q.relation_revision,
                       q.is_active AS relation_is_active,
                       x.id AS rejection_id, x.rejection_revision
                FROM coverage_lines l
                LEFT JOIN coverage_analyses a ON a.line_id = l.id
                  LEFT JOIN coverage_analysis_line_links q
                  ON q.scan_id=? AND q.line_id=l.id
                LEFT JOIN coverage_analysis_records r ON r.id=q.analysis_record_id
                LEFT JOIN coverage_inheritance_rejections x
                  ON x.scan_id=? AND x.line_id=l.id AND x.is_active=1
                WHERE l.file_id = ?
                ORDER BY l.line_number
                LIMIT ? OFFSET ?
            """, (int(scan_id), int(scan_id), int(file_row["id"]), page_size, offset))
        total = int((total_row or {}).get("total") or 0)
        return 200, {
            "page": page, "page_size": page_size, "total": total,
            "total_pages": (total + page_size - 1) // page_size if total else 0,
            "rows": rows,
        }

    def progress_pending(self, query, body, headers, remote_address):
        project_name = progress_endpoint.project_name(
            query, self.config.get("project_name") or ""
        )
        scan_id = query.get("scan_id")
        page = max(1, int(query.get("page") or 1))
        page_size = min(500, max(1, int(query.get("page_size") or 100)))
        with self._read_connection() as connection:
            return 200, self.runtime.progress_service.pending_page(
                connection, project_name,
                int(scan_id) if scan_id else None,
                page=page, page_size=page_size,
            )

    def unanalyzed(self, query, body, headers, remote_address):
        project_name = progress_endpoint.project_name(query, self.config.get("project_name") or "")
        with self._read_connection() as connection:
            return 200, {
                "project_name": project_name,
                "scan_id": int(query.get("scan_id")) if query.get("scan_id") else None,
                "files": self.runtime.progress_service.pending_by_file(
                    connection, project_name,
                    int(query.get("scan_id")) if query.get("scan_id") else None,
                ),
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
        scan_id, report_id, repository_name, file_path = self._code_detail_args(query)
        with self._read_connection() as connection:
            return 200, self.runtime.code_detail.layout(
                connection, scan_id, report_id, repository_name, file_path
            )

    def code_lines(self, query, body, headers, remote_address):
        scan_id, report_id, repository_name, file_path = self._code_detail_args(query)
        start_line = int(query.get("start_line") or 1)
        end_line = int(query.get("end_line") or start_line)
        with self._read_connection() as connection:
            return 200, self.runtime.code_detail.lines(
                connection, scan_id, report_id, repository_name, file_path,
                start_line, end_line
            )

    def code_lines_batch(self, query, body, headers, remote_address):
        scan_id, report_id, repository_name, file_path = self._code_detail_args(body)
        ranges = body.get("ranges") or []
        if not isinstance(ranges, list) or len(ranges) > 1000:
            raise ValueError("ranges must be a list with at most 1000 entries")
        if not ranges:
            return 200, {
                "scan_id": scan_id, "report_id": report_id,
                "repository_name": repository_name, "file_path": file_path,
                "batches": [],
            }
        with self._read_connection() as connection:
            result = self.runtime.code_detail.lines_batch(
                connection, scan_id, report_id, repository_name, file_path, ranges
            )
        return 200, {
            "scan_id": scan_id, "report_id": report_id,
            "repository_name": repository_name, "file_path": file_path,
            "batches": result,
        }

    def create_project(self, query, body, headers, remote_address):
        self._require_mutation(headers, remote_address)
        project_name = projects_endpoint.project_name(body)
        with self._write_connection() as connection:
            row = self.runtime.project_service.ensure_project(connection, project_name)
            return 201, {"project": row}

    def create_scan(self, query, body, headers, remote_address):
        identity = self._require_role(headers, remote_address, ("importer", "admin"))
        project_name = projects_endpoint.project_name(body)
        with self._write_connection() as connection:
            if body.get("info_path"):
                repositories = [dict(item or {}) for item in (body.get("repositories") or [])]
                resource_ids = []
                for repository in repositories:
                    resource_id = repository.get("physical_resource_id")
                    repository_path = repository.get("repository_path") or ""
                    if resource_id is None and repository_path:
                        resolved = self.runtime.repository_repository.resolve_git_resource(
                            repository_path
                        )
                        resource = self.runtime.repository_repository.ensure_resource(
                            connection, resolved["common_dir"], resolved["worktree_root"],
                            fs_stat=resolved["stat"],
                        )
                        resource_id = resource["id"]
                        repository["physical_resource_id"] = resource_id
                    if resource_id is None:
                        raise ValueError("physical repository resource is required")
                    resource_ids.append(int(resource_id))
                if not resource_ids:
                    raise ValueError("physical repository resource is required")
                durable = self.runtime.scan_import_coordinator.create(
                    connection, project_name, body["info_path"],
                    info_sha256=body.get("info_sha256", ""),
                    repository_resource_ids=resource_ids,
                    repositories=repositories,
                    review_scope=body.get("review_scope", "full"),
                    report=body.get("report"),
                    requested_by=identity,
                    staging_root=self.runtime.scan_import_staging_root,
                    algorithm_version=body.get("algorithm_version", "vnext-import-v1"),
                )
                import_job = durable["job"]
                import_job_id = str(import_job["job_id"])
                import_owner = durable.get("owner_token") or ""
                import_locks = durable.get("locks") or []
                import_fence = int(
                    import_locks[0].get("fencing_token")
                    if import_locks else 0
                )

                def import_callback():
                    with self._write_connection() as callback_connection:
                        return self.runtime.scan_import_coordinator.execute(
                            callback_connection, import_job_id,
                            owner_token=import_owner,
                            fencing_token=import_fence,
                        )

                try:
                    queued_job = self.runtime.job_service.submit(
                        project_id=import_job["project_id"],
                        scan_id=import_job["scan_id"],
                        kind="scan_import",
                        data_version=int(import_job.get("data_version") or 0),
                        callback=import_callback,
                        input_payload=json.loads(
                            import_job.get("input_payload") or "{}"
                        ),
                        job_id=import_job_id,
                        resource_class="database",
                    )
                except Exception as exc:
                    self.runtime.scan_import_recovery.record_failure(
                        connection, import_job_id, "ENQUEUE", exc.__class__.__name__,
                        str(exc), fencing_token=import_fence,
                    )
                    raise
                # Internal lock ownership and filesystem paths are not API
                # credentials.  Keep them out of the response while retaining
                # the durable job/checkpoint/artifact identity for polling.
                response = dict(durable)
                response["job"] = queued_job
                response.pop("owner_token", None)
                response.pop("locks", None)
                return 202, response
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
        identity = self._require_role(headers, remote_address, ("reviewer", "admin"))
        project_name, scan_id, records = analysis_endpoint.request(body)
        # Do not trust a reviewer value supplied in JSON. The Git
        # ``suggested_reviewer`` is separate metadata, and the saved DB
        # reviewer is the authenticated operator who confirmed the analysis.
        records = [dict(item, reviewer=identity) for item in records]
        with self._write_connection() as connection:
            result = self.runtime.analysis_service.save(
                connection, project_name, scan_id, records,
                reviewer=identity,
                enforce_current=True,
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
