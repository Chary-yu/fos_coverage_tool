"""VNext incremental orchestration and Scan-bound result persistence."""

import os

from app.db.repositories import IncrementalRepository, ProjectRepository
from app.db.transaction import transaction
from app.incremental.orchestrator import IncrementalOrchestrator
from app.config.path_policy import realpath_within, reject_relative_traversal
from app.reports.compatibility import with_report_compatibility


class IncrementalReportService(object):
    def __init__(self, project_repo=None, result_repo=None, orchestrator=None,
                 release_identity=None, api_contract_version=None):
        self.projects = project_repo or ProjectRepository()
        self.results = result_repo or IncrementalRepository()
        self.orchestrator = orchestrator or IncrementalOrchestrator()
        self.allowed_roots = []
        self.release_identity = dict(release_identity or {})
        self.api_contract_version = api_contract_version

    def build_and_persist(self, connection, project_name, scan_id, repo_path,
                          oldgit, newgit, info_path, repository_name="default",
                          report_id=""):
        reject_relative_traversal(repo_path)
        reject_relative_traversal(info_path)
        if self.allowed_roots:
            if not realpath_within(repo_path, self.allowed_roots) or \
                    not realpath_within(info_path, self.allowed_roots):
                raise ValueError("incremental input is outside configured roots")
        project = self.projects.get_project_by_name(connection, project_name)
        scan = self.projects.get_scan(connection, int(scan_id))
        if not project or not scan or int(scan["project_id"]) != int(project["id"]):
            raise KeyError("scan is not bound to project")
        snapshots = self.projects.list_repository_snapshots(connection, int(scan_id))
        snapshot = next((item for item in snapshots
                         if item.get("repository_name") == repository_name), None)
        if snapshot and int(snapshot.get("verified") or 0):
            if (snapshot.get("old_commit_sha") or "") != (oldgit or "") or \
                    (snapshot.get("new_commit_sha") or "") != (newgit or ""):
                raise ValueError("Git range does not match immutable Scan snapshot")
            configured_root = snapshot.get("repository_path") or ""
            if configured_root and os.path.realpath(repo_path) != os.path.realpath(configured_root):
                raise ValueError("repo_path does not match immutable repository snapshot")
        report = self.orchestrator.build(
            project_name, repo_path, oldgit, newgit, info_path,
            repository_name=repository_name, scan_id=scan_id, report_id=report_id,
        )
        report = with_report_compatibility(
            report, self.release_identity,
            api_contract_version=self.api_contract_version,
        )
        with transaction(connection) as conn:
            row = self.results.upsert(
                conn, scan_id, report_id or report.get("report_id", ""),
                repository_name, report,
            )
        return {"result": report, "stored": row}
