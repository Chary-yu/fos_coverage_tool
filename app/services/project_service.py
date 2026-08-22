"""Project and immutable Scan lifecycle service."""

import hashlib
import json
import os

from app.code_detail.source_reader import compute_db_file_path_hash
from app.config.path_policy import realpath_within, reject_relative_traversal
from app.db.repositories import (
    ProjectRepository, ProjectStateRepository, LineIndexRepository,
    RepositoryRepository,
)
from app.db.transaction import transaction
from app.reports.identity import validate_report_id
from app.scan_import.publication import ScanPublicationService


CONSTRUCTION_STATUSES = {"building", "importing", "constructing"}


class ProjectService(object):
    def __init__(self, project_repo=None, state_repo=None, line_repo=None,
                 allowed_report_roots=None, repository_repo=None):
        self.projects = project_repo or ProjectRepository()
        self.states = state_repo or ProjectStateRepository()
        self.lines = line_repo or LineIndexRepository()
        self.repositories = repository_repo or RepositoryRepository()
        self.publication = ScanPublicationService(self.states)
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

    @staticmethod
    def _file_hash(path, repository_name=""):
        return compute_db_file_path_hash(path, repository_name)

    def ensure_project(self, connection, project_name):
        with transaction(connection) as conn:
            project = self.projects.ensure_project(conn, project_name)
            self.states.ensure(conn, project["id"])
        return project

    @staticmethod
    def scan_key(project_name, info_sha256, repositories, review_scope,
                 report_source_signature=""):
        repositories = sorted(
            [{
                "repository_name": str((item or {}).get("repository_name") or ""),
                "branch_name": str((item or {}).get("branch_name") or ""),
                "commit_sha": str((item or {}).get("commit_sha") or ""),
                "old_commit_sha": str((item or {}).get("old_commit_sha") or ""),
                "new_commit_sha": str((item or {}).get("new_commit_sha") or ""),
            } for item in (repositories or [])],
            key=lambda item: (
                str(item.get("repository_name") or ""),
                str(item.get("branch_name") or ""),
                str(item.get("commit_sha") or ""),
                str(item.get("old_commit_sha") or ""),
                str(item.get("new_commit_sha") or ""),
            ),
        )
        payload = {
            "project": project_name, "info_sha256": info_sha256 or "",
            "review_scope": review_scope or "full", "repositories": repositories,
            "report_source_signature": report_source_signature or "",
            "identity_contract_version": 3,
        }
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    def _validate_existing_scan(self, connection, scan, repositories, report):
        expected = sorted(str(item.get("repository_name") or "")
                          for item in (repositories or []))
        actual = sorted(str(item.get("repository_name") or "")
                        for item in self.projects.list_repository_snapshots(
                            connection, scan["id"]))
        if expected != actual:
            raise ValueError("existing scan identity does not match repository snapshots")
        if report:
            bound = self.projects.get_report_for_scan(connection, scan["id"])
            if not bound or bound.get("report_id") != report.get("report_id"):
                raise ValueError("existing scan identity does not match report")

    def create_scan(self, connection, project_name, info_file_name="", info_sha256="",
                    review_scope="full", repositories=None, report=None,
                    scan_type="import", status="building"):
        repositories = list(repositories or [])
        scan_key = self.scan_key(
            project_name, info_sha256, repositories, review_scope,
            (report or {}).get("source_signature", "") if report else "",
        )
        with transaction(connection) as conn:
            project = self.projects.ensure_project(conn, project_name)
            existing = self.projects.get_scan_by_key(conn, scan_key)
            if existing:
                self._validate_existing_scan(conn, existing, repositories, report)
                self.states.ensure(conn, project["id"])
                return existing
            previous_state = self.states.get(conn, project["id"])
            predecessor_scan_id = (previous_state or {}).get("current_scan_id")
            scan = self.projects.create_scan(
                conn, project["id"], scan_key, scan_type, review_scope,
                info_file_name=info_file_name, info_sha256=info_sha256,
                status=status, predecessor_scan_id=predecessor_scan_id,
                algorithm_version="vnext-scan-identity-v3",
            )
            for snapshot in repositories:
                repository_id = None
                repository_name = snapshot.get("repository_name", "")
                if repository_name:
                    repository = self.repositories.ensure(
                        conn, project["id"], repository_name,
                        canonical_remote=snapshot.get("canonical_remote", ""),
                        physical_resource_id=snapshot.get("physical_resource_id"),
                        physical_path=snapshot.get("repository_path", ""),
                    )
                    repository_id = repository["id"]
                self.projects.upsert_repository_snapshot(
                    conn, scan["id"], repository_name,
                    repository_path=snapshot.get("repository_path", ""),
                    branch_name=snapshot.get("branch_name", ""),
                    old_commit_sha=snapshot.get("old_commit_sha"),
                    new_commit_sha=snapshot.get("new_commit_sha"),
                    verified=int(bool(snapshot.get("verified"))),
                    provenance=snapshot.get("provenance", ""),
                    repository_id=repository_id,
                    commit_sha=snapshot.get("commit_sha"),
                    identity_verified=int(bool(snapshot.get("identity_verified", False))),
                    identity_provenance=snapshot.get("identity_provenance", ""),
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
            self.states.ensure(conn, project["id"])
            self.states.advance(conn, project["id"])
        return self.get_scan(connection, scan["id"])

    def _ingest_files(self, connection, scan_id, files):
        self.projects._assert_scan_building(connection, scan_id)
        normalized = []
        for item in files or []:
            path = str(item.get("file_path") or "")
            repository_name = str(item.get("repository_name") or "")
            file_hash = str(item.get("file_path_hash") or "")
            if not file_hash:
                file_hash = self._file_hash(path, repository_name)
            normalized.append({
                "repository_name": repository_name,
                "file_path_hash": file_hash,
                "file_path": path,
                "source_file_name": item.get("source_file_name") or os.path.basename(path),
                "lines": item.get("lines") or [],
            })
        file_rows = self.projects.ensure_files(connection, scan_id, normalized)
        for item in normalized:
            file_row = file_rows[(item["repository_name"], item["file_path_hash"])]
            self.lines.upsert_lines(connection, file_row["id"], item.get("lines") or [])

    def create_scan_and_ingest(self, connection, project_name, files,
                               info_file_name="", info_sha256="", review_scope="full",
                               repositories=None, report=None, scan_type="import",
                               status="building"):
        """Create, populate and seal one immutable scan transactionally."""
        repositories = list(repositories or [])
        scan_key = self.scan_key(
            project_name, info_sha256, repositories, review_scope,
            (report or {}).get("source_signature", "") if report else "",
        )
        with transaction(connection) as conn:
            project = self.projects.ensure_project(conn, project_name)
            existing = self.projects.get_scan_by_key(conn, scan_key)
            if existing:
                self._validate_existing_scan(conn, existing, repositories, report)
                self.states.ensure(conn, project["id"])
                return existing
            previous_state = self.states.get(conn, project["id"])
            predecessor_scan_id = (previous_state or {}).get("current_scan_id")
            scan = self.projects.create_scan(
                conn, project["id"], scan_key, scan_type, review_scope,
                info_file_name=info_file_name, info_sha256=info_sha256,
                status=status, predecessor_scan_id=predecessor_scan_id,
                algorithm_version="vnext-scan-identity-v3",
            )
            for snapshot in repositories:
                repository_id = None
                repository_name = snapshot.get("repository_name", "")
                if repository_name:
                    repository = self.repositories.ensure(
                        conn, project["id"], repository_name,
                        canonical_remote=snapshot.get("canonical_remote", ""),
                        physical_resource_id=snapshot.get("physical_resource_id"),
                        physical_path=snapshot.get("repository_path", ""),
                    )
                    repository_id = repository["id"]
                self.projects.upsert_repository_snapshot(
                    conn, scan["id"], repository_name,
                    repository_path=snapshot.get("repository_path", ""),
                    branch_name=snapshot.get("branch_name", ""),
                    old_commit_sha=snapshot.get("old_commit_sha"),
                    new_commit_sha=snapshot.get("new_commit_sha"),
                    verified=int(bool(snapshot.get("verified"))),
                    provenance=snapshot.get("provenance", ""),
                    repository_id=repository_id,
                    commit_sha=snapshot.get("commit_sha"),
                    identity_verified=int(bool(snapshot.get("identity_verified", False))),
                    identity_provenance=snapshot.get("identity_provenance", ""),
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
            self._ingest_files(conn, scan["id"], files)
            self.states.ensure(conn, project["id"])
            self.states.advance(conn, project["id"])
            self.projects.seal_scan(conn, scan["id"])
            # Synchronous compatibility API: the actual pointer mutation is
            # still owned by ScanPublicationService and happens only after
            # the candidate is fully sealed. Durable imports call it later.
            self.publication.publish_in_transaction(
                conn, project["id"], scan["id"],
                expected_current_scan_id=(self.states.get(conn, project["id"]) or {}).get(
                    "current_scan_id"
                ),
            )
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
        return self.projects.list_scans(connection, project["id"]) if project else []

    def ingest_files(self, connection, scan_id, files):
        """Compatibility construction API; seals the scan after one batch."""
        scan = self.get_scan(connection, scan_id)
        project = self.projects.get_project(connection, int(scan["project_id"]))
        with transaction(connection) as conn:
            self._ingest_files(conn, scan_id, files)
            self.states.ensure(conn, project["id"])
            self.states.advance(conn, project["id"])
            self.projects.seal_scan(conn, scan_id)
            self.publication.publish_in_transaction(
                conn, project["id"], scan_id,
                expected_current_scan_id=(self.states.get(conn, project["id"]) or {}).get(
                    "current_scan_id"
                ),
            )
        return {"scan_id": int(scan_id), "files": len(files or [])}

    def seal_scan(self, connection, scan_id):
        with transaction(connection) as conn:
            self.projects.seal_scan(conn, scan_id)
        return self.get_scan(connection, scan_id)
