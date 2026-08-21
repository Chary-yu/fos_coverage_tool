import os
import sqlite3
import tempfile
import unittest

from app.scan_import import (
    RepositoryBusyError, RepositoryResourceLockService, ScanImportCoordinator,
)
from app.db.repositories import (
    AnalysisDomainRepository, FileStateRepository, LineIndexRepository,
    ProjectRepository, ProjectStateRepository, RepositoryRepository,
)
from app.inheritance.engine import InheritanceEngine
from app.inject.service import ScanImportService
from app.services.analysis_domain_service import AnalysisDomainService
from app.services.project_service import ProjectService
from scripts.upgrade.migration_runner import create_sqlite_schema


class ScanImportLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_sqlite_schema(self.connection)
        self.connection.execute("""
            INSERT INTO coverage_repository_resources(
                resource_key, resolved_git_common_dir, resolved_worktree_root,
                next_fencing_token, observed_at
            ) VALUES ('r', '/tmp/common', '/tmp/worktree', 0, CURRENT_TIMESTAMP)
        """)
        self.connection.commit()
        self.resource_id = self.connection.execute(
            "SELECT id FROM coverage_repository_resources"
        ).fetchone()[0]
        self.temp = tempfile.TemporaryDirectory(prefix="scan-import-")
        self.info_path = os.path.join(self.temp.name, "coverage.info")
        with open(self.info_path, "w") as stream:
            stream.write("TN:\nSF:src/a.c\nDA:1,0\nend_of_record\n")

    def tearDown(self):
        self.temp.cleanup()
        self.connection.close()

    def test_busy_resource_has_no_candidate_scan_residue(self):
        lock = RepositoryResourceLockService()
        first = lock.acquire(self.connection, [self.resource_id], "job-a", "owner-a")
        self.assertEqual(first[0]["fencing_token"], 1)
        self.connection.commit()
        coordinator = ScanImportCoordinator()
        with self.assertRaises(RepositoryBusyError):
            coordinator.create(
                self.connection, "busy", self.info_path,
                repository_resource_ids=[self.resource_id],
                staging_root=os.path.join(self.temp.name, "stage"),
            )
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM coverage_scans"
        ).fetchone()[0], 0)
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM coverage_background_jobs"
        ).fetchone()[0], 0)
        lock.release(self.connection, "job-a", "owner-a")
        self.connection.commit()

    def test_staged_import_has_fixed_predecessor_checkpoint_and_atomic_publish(self):
        coordinator = ScanImportCoordinator()
        result = coordinator.create(
            self.connection, "candidate", self.info_path,
            repository_resource_ids=[self.resource_id],
            staging_root=os.path.join(self.temp.name, "stage"),
            requested_by="operator",
        )
        self.assertIsNone(self.connection.execute(
            "SELECT current_scan_id FROM coverage_project_state"
        ).fetchone()[0])
        self.assertEqual(result["checkpoint"]["phase"], "LOCKED")
        self.assertEqual(result["checkpoint"]["expected_current_scan_id"], None)
        self.assertTrue(os.path.isfile(result["artifact"]["staged_path"]))

        checkpoint = result["checkpoint"]
        phases = ("SCAN_CREATED", "INFO_STAGED", "COVERAGE_IMPORTED",
                  "GIT_VERIFIED", "SOURCE_PREPARED", "LINE_MAP_BUILT",
                  "INHERITANCE_COMPUTED", "STATS_REBUILT",
                  "CONSISTENCY_VERIFIED")
        for index, phase in enumerate(phases):
            checkpoint = coordinator.advance(
                self.connection, result["job"]["job_id"], index,
                result["locks"][0]["fencing_token"], phase,
            )
        sealed = coordinator.seal(
            self.connection, result["job"]["job_id"], result["owner_token"],
            result["locks"][0]["fencing_token"],
        )
        self.assertEqual(sealed["phase"], "SEALED")
        state = coordinator.publish(self.connection, result["job"]["job_id"])
        scan_id = result["scan"]["id"]
        self.assertEqual(state["current_scan_id"], scan_id)
        self.assertEqual(self.connection.execute(
            "SELECT status FROM coverage_scans WHERE id=?", (scan_id,)
        ).fetchone()[0], "SEALED")
        self.assertEqual(coordinator.checkpoints.get(
            self.connection, result["job"]["job_id"]
        )["phase"], "PUBLISHED")

    def test_fencing_tokens_are_monotonic_after_release(self):
        service = RepositoryResourceLockService()
        first = service.acquire(self.connection, [self.resource_id], "job-1", "owner-1")
        service.release(self.connection, "job-1", "owner-1")
        second = service.acquire(self.connection, [self.resource_id], "job-2", "owner-2")
        self.assertGreater(second[0]["fencing_token"], first[0]["fencing_token"])

    def test_execute_imports_staged_info_computes_inheritance_and_publishes(self):
        projects = ProjectRepository()
        states = ProjectStateRepository()
        repositories = RepositoryRepository()
        project_service = ProjectService(
            projects, states, LineIndexRepository(), repository_repo=repositories
        )
        domain_repository = AnalysisDomainRepository()
        coordinator = ScanImportCoordinator(
            project_repository=projects,
            state_repository=states,
            repository_repository=repositories,
            project_service=project_service,
            import_service=ScanImportService(project_service),
            inheritance_engine=InheritanceEngine(domain_repository=domain_repository),
            file_state_repository=FileStateRepository(),
            analysis_domain_service=AnalysisDomainService(domain_repository),
        )
        result = coordinator.create(
            self.connection, "execute", self.info_path,
            repository_resource_ids=[self.resource_id],
            staging_root=os.path.join(self.temp.name, "execute-stage"),
        )
        state = coordinator.execute(
            self.connection, result["job"]["job_id"],
            owner_token=result["owner_token"],
            fencing_token=result["locks"][0]["fencing_token"],
        )
        self.assertEqual(state["current_scan_id"], result["scan"]["id"])
        self.assertEqual(self.connection.execute(
            "SELECT status FROM coverage_scans WHERE id=?",
            (result["scan"]["id"],),
        ).fetchone()[0], "SEALED")
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM coverage_inheritance_decisions "
            "WHERE candidate_scan_id=?", (result["scan"]["id"],)
        ).fetchone()[0], 1)
        self.assertEqual(self.connection.execute(
            "SELECT pending_total FROM coverage_file_state WHERE scan_id=?",
            (result["scan"]["id"],),
        ).fetchone()[0], 1)
        self.assertEqual(coordinator.checkpoints.get(
            self.connection, result["job"]["job_id"]
        )["phase"], "PUBLISHED")


if __name__ == "__main__":
    unittest.main()
