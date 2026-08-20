"""Project and immutable Scan lifecycle service."""

import hashlib
import json
import os

from app.db.repositories import ProjectRepository, ProjectStateRepository, LineIndexRepository
from app.db.transaction import transaction
from app.reports.identity import validate_report_id
from app.config.path_policy import realpath_within, reject_relative_traversal


class ProjectService(object):
    def __init__(self, project_repo=None, state_repo=None, line_repo=None,
                 allowed_report_roots=None):
        self.projects = project_repo or ProjectRepository()
        self.states = state_repo or ProjectStateRepository()
        self.lines = line_repo or LineIndexRepository()
        self.allowed_report_roots = [os.path.realpath(root) for root in
                                     (allowed_report_roots or [])]

    def _validate_report(self, report):
        if not report:
            return
        validate_report_id(report.get("report_id"))
        roots = [report.get("report_root")] + list(report.get("directories") or [])
        for root in roots:
            if not root:
                continue
            reject_relative_traversal(root)
            if self.allowed_report_roots and not realpath_within(
                    root, self.allowed_report_roots):
                raise ValueError("report root is outside configured report roots")

    def ensure_project(self, connection, project_name):
        """Create/read a project and its authoritative state atomically."""
        with transaction(connection) as conn:
            project = self.projects.ensure_project(conn, project_name)
            self.states.ensure(conn, project["id"])
        return project

    @staticmethod
    def scan_key(project_name, info_sha256, repositories, review_scope):
        repositories = sorted(
            [dict(item or {}) for item in (repositories or [])],
            key=lambda item: (
                str(item.get("repository_name") or ""),
                str(item.get("old_commit_sha") or ""),
                str(item.get("new_commit_sha") or ""),
            ),
        )
        payload = {
            "project": project_name,
            "info_sha256": info_sha256 or "",
            "review_scope": review_scope or "full",
            "repositories": repositories,
        }
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    def create_scan(self, connection, project_name, info_file_name="", info_sha256="",
                    review_scope="full", repositories=None, report=None,
                    scan_type="import", status="ready"):
        repositories = list(repositories or [])
        scan_key = self.scan_key(project_name, info_sha256, repositories, review_scope)
        with transaction(connection) as conn:
            project = self.projects.ensure_project(conn, project_name)
            existing = self.projects.get_scan_by_key(conn, scan_key)
            scan = self.projects.create_scan(
                conn, project["id"], scan_key, scan_type, review_scope,
                info_file_name=info_file_name, info_sha256=info_sha256,
                status=status,
            )
            for snapshot in repositories:
                self.projects.upsert_repository_snapshot(
                    conn, scan["id"], snapshot.get("repository_name", ""),
                    repository_path=snapshot.get("repository_path", ""),
                    branch_name=snapshot.get("branch_name", ""),
                    old_commit_sha=snapshot.get("old_commit_sha"),
                    new_commit_sha=snapshot.get("new_commit_sha"),
                    verified=int(bool(snapshot.get("verified"))),
                    provenance=snapshot.get("provenance", ""),
                )
            if report:
                self._validate_report(report)
                self.projects.bind_report(conn, scan["id"], report["report_id"],
                                          report_root=report.get("report_root", ""),
                                          source_signature=report.get("source_signature", ""),
                                          sidecar_schema=report.get("sidecar_schema", 0),
                                          asset_identity=report.get("asset_identity", ""))
            self.states.ensure(conn, project["id"], current_scan_id=scan["id"])
            if existing is None:
                self.states.advance(conn, project["id"])
            self.states.set_current_scan(conn, project["id"], scan["id"])
        return self.get_scan(connection, scan["id"])

    def create_scan_and_ingest(self, connection, project_name, files,
                               info_file_name="", info_sha256="", review_scope="full",
                               repositories=None, report=None, scan_type="import",
                               status="ready"):
        """Create an immutable scan and its physical line facts in one transaction."""
        repositories = list(repositories or [])
        scan_key = self.scan_key(project_name, info_sha256, repositories, review_scope)
        with transaction(connection) as conn:
            project = self.projects.ensure_project(conn, project_name)
            existing = self.projects.get_scan_by_key(conn, scan_key)
            scan = self.projects.create_scan(
                conn, project["id"], scan_key, scan_type, review_scope,
                info_file_name=info_file_name, info_sha256=info_sha256,
                status=status,
            )
            for snapshot in repositories:
                self.projects.upsert_repository_snapshot(
                    conn, scan["id"], snapshot.get("repository_name", ""),
                    repository_path=snapshot.get("repository_path", ""),
                    branch_name=snapshot.get("branch_name", ""),
                    old_commit_sha=snapshot.get("old_commit_sha"),
                    new_commit_sha=snapshot.get("new_commit_sha"),
                    verified=int(bool(snapshot.get("verified"))),
                    provenance=snapshot.get("provenance", ""),
                )
            if report:
                self._validate_report(report)
                self.projects.bind_report(
                    conn, scan["id"], report["report_id"],
                    report_root=report.get("report_root", ""),
                    source_signature=report.get("source_signature", ""),
                    sidecar_schema=report.get("sidecar_schema", 0),
                    asset_identity=report.get("asset_identity", ""),
                )
            for item in files or []:
                path = str(item.get("file_path") or "")
                file_hash = str(item.get("file_path_hash") or "")
                if not file_hash:
                    file_hash = hashlib.md5(path.encode("utf-8")).hexdigest()
                file_row = self.projects.ensure_file(
                    conn, scan["id"], item.get("repository_name", ""), file_hash,
                    path, item.get("source_file_name") or os.path.basename(path),
                )
                self.lines.upsert_lines(conn, file_row["id"], item.get("lines") or [])
            self.states.ensure(conn, project["id"], current_scan_id=scan["id"])
            if existing is None:
                self.states.advance(conn, project["id"])
            self.states.set_current_scan(conn, project["id"], scan["id"])
        return self.get_scan(connection, scan["id"])

    def get_scan(self, connection, scan_id):
        scan = self.projects.get_scan(connection, int(scan_id))
        if not scan:
            raise KeyError("scan not found: {}".format(scan_id))
        return scan

    def get_current_scan(self, connection, project_name):
        project = self.projects.get_project_by_name(connection, project_name)
        if not project:
            raise KeyError("project not found: {}".format(project_name))
        state = self.states.get(connection, project["id"])
        if not state or not state.get("current_scan_id"):
            return None
        return self.get_scan(connection, state["current_scan_id"])

    def list_projects(self, connection):
        return self.projects.list_projects(connection)

    def list_scans(self, connection, project_name):
        project = self.projects.get_project_by_name(connection, project_name)
        if not project:
            return []
        return self.projects.list_scans(connection, project["id"])

    def ingest_files(self, connection, scan_id, files):
        scan = self.get_scan(connection, scan_id)
        project = self.projects.get_project(connection, int(scan["project_id"]))
        with transaction(connection) as conn:
            for item in files or []:
                path = str(item.get("file_path") or "")
                file_hash = str(item.get("file_path_hash") or "")
                if not file_hash:
                    file_hash = hashlib.md5(path.encode("utf-8")).hexdigest()
                file_row = self.projects.ensure_file(
                    conn, scan["id"], item.get("repository_name", ""), file_hash,
                    path, item.get("source_file_name") or os.path.basename(path),
                )
                self.lines.upsert_lines(conn, file_row["id"], item.get("lines") or [])
            self.states.ensure(conn, project["id"], current_scan_id=scan["id"])
            self.states.advance(conn, project["id"])
            self.states.set_current_scan(conn, project["id"], scan["id"])
        return {"scan_id": int(scan_id), "files": len(files or [])}
