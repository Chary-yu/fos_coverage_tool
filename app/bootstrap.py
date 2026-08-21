"""Canonical VNext runtime composition root."""

import os
from contextlib import contextmanager

from app.api.application import VNextApplication
from app.db.manager import DatabaseManager
from app.db.repositories import (
    AnalysisRepository,
    FileStateRepository,
    IncrementalRepository,
    JobRepository,
    LineIndexRepository,
    ProjectRepository,
    ProjectStateRepository,
    RepositoryRepository,
    AnalysisDomainRepository,
)
from app.release_identity import generate_release_identity, get_current_release_identity
from app.inject.service import ScanImportService
from app.code_detail.vnext_service import VNextCodeDetailService
from app.reports.registry import ReportRegistry
from app.services.analysis_service import AnalysisService
from app.services.analysis_domain_service import AnalysisDomainService
from app.services.repository_service import RepositoryService
from app.scan_import import (
    ImmutableArtifactStager, ScanImportCoordinator, ScanImportRecoveryService,
    ScanPublicationService,
)
from app.services.project_service import ProjectService
from app.services.progress_service import ProgressService
from app.services.export_service import ExportService
from app.services.incremental_service import IncrementalReportService
from app.inheritance.engine import InheritanceEngine
from app.jobs.bounded_executor import BoundedJobExecutor
from app.jobs.service import VNextBackgroundJobService


class _ConnectionLease(object):
    """DB-API proxy whose close returns a pooled connection or does nothing."""

    def __init__(self, connection, close_callback=None):
        self._connection = connection
        self._close_callback = close_callback
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._close_callback:
            self._close_callback()


class VNextRuntime(object):
    def __init__(self, config, repo_root, connection=None, database_manager=None,
                 connection_factory=None):
        self.config = config or {}
        self.repo_root = os.path.realpath(repo_root)
        if connection is not None and connection_factory is not None:
            raise ValueError("connection and connection_factory are mutually exclusive")
        self._connection = connection
        self._connection_factory = connection_factory
        self.database_manager = database_manager
        self._closed = False
        self.projects = ProjectRepository()
        self.lines = LineIndexRepository()
        self.analyses = AnalysisRepository()
        self.states = ProjectStateRepository()
        self.file_states = FileStateRepository()
        self.job_repository = JobRepository()
        self.incremental_results = IncrementalRepository()
        self.repository_repository = RepositoryRepository()
        self.analysis_domain_repository = AnalysisDomainRepository()
        self.repository_service = RepositoryService(self.repository_repository)
        self.analysis_domain_service = AnalysisDomainService(self.analysis_domain_repository)
        self.scan_publication_service = ScanPublicationService(self.states)
        # ``jobs`` remains a read/query compatibility attribute; lifecycle
        # mutations go through the durable service below.
        self.jobs = self.job_repository
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
            self.projects, self.analyses, self.report_registry,
            domain_repo=self.analysis_domain_repository,
        )
        report_roots = []
        for root in self.config.get("report_roots") or []:
            root = str(root)
            report_roots.append(root if os.path.isabs(root) else os.path.join(self.repo_root, root))
        self.project_service = ProjectService(
            self.projects, self.states, self.lines,
            allowed_report_roots=report_roots,
            repository_repo=self.repository_repository,
        )
        input_roots = []
        for root in self.config.get("input_roots") or []:
            root = str(root)
            input_roots.append(root if os.path.isabs(root) else os.path.join(self.repo_root, root))
        self.scan_import_service = ScanImportService(
            self.project_service, report_registry=self.report_registry,
            allowed_info_roots=input_roots, allowed_report_roots=report_roots,
        )
        self.analysis_service = AnalysisService(
            self.analyses, self.projects, self.states, self.lines,
            domain_repo=self.analysis_domain_repository,
        )
        state_root = state_config.get("root") or os.path.join(self.repo_root, ".runtime-state")
        if not os.path.isabs(state_root):
            state_root = os.path.join(self.repo_root, state_root)
        import_root = state_config.get("import_staging_dir") or os.path.join(
            state_root, "import-staging"
        )
        if not os.path.isabs(import_root):
            import_root = os.path.join(self.repo_root, import_root)
        self.scan_import_coordinator = ScanImportCoordinator(
            project_repository=self.projects, state_repository=self.states,
            publication_service=self.scan_publication_service,
            stager=ImmutableArtifactStager(import_root),
            repository_repository=self.repository_repository,
            import_service=self.scan_import_service,
            inheritance_engine=InheritanceEngine(
                domain_repository=self.analysis_domain_repository,
            ),
            file_state_repository=self.file_states,
            analysis_domain_service=self.analysis_domain_service,
            project_service=self.project_service,
        )
        self.scan_import_staging_root = import_root
        self.scan_import_recovery = ScanImportRecoveryService(
            coordinator=self.scan_import_coordinator,
            jobs=self.job_repository,
            projects=self.projects,
            stager=ImmutableArtifactStager(import_root),
        )
        self.progress_service = ProgressService(self.file_states, self.projects, self.states)
        self.incremental_service = IncrementalReportService(
            self.projects, self.incremental_results
        )
        self.incremental_service.allowed_roots = list(input_roots)
        export_root = state_config.get("exports_dir") or os.path.join(state_root, "exports")
        if not os.path.isabs(export_root):
            export_root = os.path.join(self.repo_root, export_root)
        job_config = self.config.get("jobs") or {}
        resource_limits = job_config.get("resource_limits")
        if resource_limits is None:
            worker_count = max(1, int(job_config.get("max_workers", 4)))
            queue_size = max(1, int(job_config.get("max_queue_size", 100)))
            resource_limits = {
                "database": {"max_workers": max(1, min(2, worker_count)),
                              "max_queue_size": max(1, min(queue_size, 25))},
                "cpu": {"max_workers": worker_count,
                        "max_queue_size": max(1, min(queue_size, 50))},
                "disk": {"max_workers": max(1, min(2, worker_count)),
                         "max_queue_size": max(1, min(queue_size, 25))},
            }
        self.job_service = VNextBackgroundJobService(
            self._job_connection,
            repository=self.job_repository,
            executor=BoundedJobExecutor(
                max_workers=int(job_config.get("max_workers", 4)),
                max_queue_size=int(job_config.get("max_queue_size", 100)),
                resource_limits=resource_limits,
                global_worker_budget=job_config.get("global_worker_budget"),
            ),
            heartbeat_timeout=float(job_config.get("heartbeat_timeout", 300)),
            heartbeat_interval=float(job_config.get("heartbeat_interval", 15)),
            lease_owner=job_config.get("lease_owner"),
        )
        # scan_import has its own checkpoint/fencing recovery owner.  The
        # generic stale-job reaper must not silently mark those candidates
        # interrupted before the dedicated service can validate and reclaim
        # them.
        self.recovered_jobs = self.job_service.recover(
            exclude_kinds=("scan_import",)
        )
        self.recoverable_scan_imports = []
        with self.connection_context(read_only=True) as recovery_connection:
            self.recoverable_scan_imports = self.scan_import_recovery.list_recoverable(
                recovery_connection
            )
        self.resumed_scan_imports = []
        for recoverable in self.recoverable_scan_imports:
            self._resume_scan_import(recoverable)
        try:
            self.release_identity = get_current_release_identity(self.repo_root)
        except Exception:
            self.release_identity = generate_release_identity(repo_root=self.repo_root)
        self.export_service = ExportService(
            self.projects, export_root, release_identity=self.release_identity
        )

    def _resume_scan_import(self, job):
        """Reclaim and resume one durable import after a process restart."""
        job_id = str(job.get("job_id") or "")
        payload = {}
        try:
            import json
            payload = json.loads(job.get("input_payload") or "{}")
        except (TypeError, ValueError):
            payload = {}

        def callback():
            with self.connection_context(read_only=False) as connection:
                reclaimed = self.scan_import_recovery.reclaim(
                    connection, job_id, self.scan_import_staging_root,
                )
                locks = reclaimed.get("locks") or []
                fence = int(locks[0].get("fencing_token") if locks else 0)
                try:
                    return self.scan_import_coordinator.execute(
                        connection, job_id,
                        owner_token=reclaimed.get("owner_token") or "",
                        fencing_token=fence,
                    )
                except Exception as exc:
                    self.scan_import_recovery.record_failure(
                        connection, job_id, "RESUME", exc.__class__.__name__,
                        str(exc), fencing_token=fence,
                    )
                    raise

        try:
            queued = self.job_service.submit(
                project_id=job.get("project_id"), scan_id=job.get("scan_id"),
                kind="scan_import", data_version=int(job.get("data_version") or 0),
                callback=callback, input_payload=payload, job_id=job_id,
                resource_class="database",
            )
            self.resumed_scan_imports.append(queued)
        except Exception as exc:
            with self.connection_context(read_only=False) as connection:
                self.scan_import_recovery.record_failure(
                    connection, job_id, "RESUME_ENQUEUE", exc.__class__.__name__,
                    str(exc),
                )

    @contextmanager
    def connection_context(self, read_only: bool = False):
        if self._connection is not None:
            yield self._connection
            return
        if self._connection_factory is not None:
            connection = self._connection_factory()
            try:
                yield connection
            finally:
                close = getattr(connection, "close", None)
                if close:
                    close()
            return
        if self.database_manager is None:
            raise RuntimeError("VNext database manager is not configured")
        with self.database_manager.connection(read_only=read_only) as connection:
            yield connection

    def _job_connection(self):
        if self._connection is not None:
            return _ConnectionLease(self._connection)
        if self._connection_factory is not None:
            connection = self._connection_factory()
            return _ConnectionLease(
                connection, close_callback=getattr(connection, "close", None)
            )
        if self.database_manager is None:
            raise RuntimeError("VNext database manager is not configured")
        pool = self.database_manager.pool
        wrapper = pool.borrow_connection()
        return _ConnectionLease(
            wrapper.raw_conn,
            close_callback=lambda: pool.return_connection(wrapper),
        )

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.job_service.shutdown()
        if self.database_manager:
            self.database_manager.close()

    def application(self):
        return VNextApplication(self, self.repo_root, self.config)


def build_runtime(config, repo_root=None, connection=None, database_manager=None,
                  connection_factory=None):
    repo_root = os.path.realpath(repo_root or os.getcwd())
    if connection is None and database_manager is None and connection_factory is None:
        database_manager = DatabaseManager(config)
    return VNextRuntime(
        config, repo_root, connection, database_manager,
        connection_factory=connection_factory,
    )


def create_vnext_server(address, config, repo_root=None, connection=None,
                        database_manager=None, connection_factory=None):
    from app.api.handler import VNextHTTPRequestHandler
    from app.api.server import create_server

    runtime = build_runtime(
        config, repo_root=repo_root, connection=connection,
        database_manager=database_manager, connection_factory=connection_factory,
    )
    handler_class = type(
        "BoundVNextHTTPRequestHandler",
        (VNextHTTPRequestHandler,),
        {"application": runtime.application()},
    )
    server = create_server(address, handler_class)
    server.vnext_runtime = runtime
    original_server_close = server.server_close
    def close_server():
        try:
            original_server_close()
        finally:
            runtime.close()
    server.server_close = close_server
    return server
