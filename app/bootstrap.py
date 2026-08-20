"""Canonical VNext runtime composition root."""

import os
from contextlib import contextmanager

from app.api.application import VNextApplication
from app.db.manager import DatabaseManager
from app.db.repositories import (
    AnalysisRepository,
    FileStateRepository,
    JobRepository,
    LineIndexRepository,
    ProjectRepository,
    ProjectStateRepository,
)
from app.release_identity import generate_release_identity, get_current_release_identity
from app.inject.service import ScanImportService
from app.code_detail.vnext_service import VNextCodeDetailService
from app.reports.registry import ReportRegistry
from app.services.analysis_service import AnalysisService
from app.services.project_service import ProjectService
from app.services.progress_service import ProgressService


class VNextRuntime(object):
    def __init__(self, config, repo_root, connection=None, database_manager=None):
        self.config = config or {}
        self.repo_root = os.path.realpath(repo_root)
        self._connection = connection
        self.database_manager = database_manager
        self.projects = ProjectRepository()
        self.lines = LineIndexRepository()
        self.analyses = AnalysisRepository()
        self.states = ProjectStateRepository()
        self.file_states = FileStateRepository()
        self.jobs = JobRepository()
        state_config = self.config.get("runtime_state") or {}
        registry_dir = state_config.get("registry_dir") or os.path.join(
            state_config.get("root") or os.path.join(self.repo_root, ".runtime-state"),
            "report-registry",
        )
        self.report_registry = ReportRegistry(
            registry_dir,
            legacy_path=os.path.join(self.repo_root, ".report_registry.json"),
        )
        self.code_detail = VNextCodeDetailService(
            self.projects, self.analyses, self.report_registry
        )
        self.project_service = ProjectService(self.projects, self.states, self.lines)
        self.scan_import_service = ScanImportService(self.project_service)
        self.analysis_service = AnalysisService(self.analyses, self.projects, self.states)
        self.progress_service = ProgressService(self.file_states, self.projects, self.states)
        try:
            self.release_identity = get_current_release_identity(self.repo_root)
        except Exception:
            self.release_identity = generate_release_identity(repo_root=self.repo_root)

    @contextmanager
    def connection_context(self):
        if self._connection is not None:
            yield self._connection
            return
        if self.database_manager is None:
            raise RuntimeError("VNext database manager is not configured")
        with self.database_manager.connection() as connection:
            yield connection

    def close(self):
        if self.database_manager:
            self.database_manager.close()

    def application(self):
        return VNextApplication(self, self.repo_root, self.config)


def build_runtime(config, repo_root=None, connection=None, database_manager=None):
    repo_root = os.path.realpath(repo_root or os.getcwd())
    if connection is None and database_manager is None:
        database_manager = DatabaseManager(config)
    return VNextRuntime(config, repo_root, connection, database_manager)


def create_vnext_server(address, config, repo_root=None, connection=None, database_manager=None):
    from app.api.handler import VNextHTTPRequestHandler
    from app.api.server import create_server

    runtime = build_runtime(
        config, repo_root=repo_root, connection=connection,
        database_manager=database_manager,
    )
    handler_class = type(
        "BoundVNextHTTPRequestHandler",
        (VNextHTTPRequestHandler,),
        {"application": runtime.application()},
    )
    server = create_server(address, handler_class)
    server.vnext_runtime = runtime
    return server
