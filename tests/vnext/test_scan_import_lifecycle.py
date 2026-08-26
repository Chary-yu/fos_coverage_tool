import os
import sqlite3
import tempfile
import unittest

from app.scan_import import (
    RepositoryBusyError, RepositoryResourceLockService, ScanImportCoordinator,
    ScanImportRecoveryService,
)
from app.db.repositories import (
    AnalysisDomainRepository, FileStateRepository, LineIndexRepository,
    ProjectRepository, ProjectStateRepository, RepositoryRepository,
)
from app.inheritance.engine import InheritanceEngine
from app.inject.service import ScanImportService
from app.services.analysis_domain_service import AnalysisDomainService
from app.services.project_service import ProjectService
from app.scan_import.publication import ScanPublicationService
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

    def test_recovery_revalidates_staged_artifact_and_rejects_stale_worker(self):
        coordinator = ScanImportCoordinator()
        result = coordinator.create(
            self.connection, "recovery", self.info_path,
            repository_resource_ids=[self.resource_id],
            staging_root=os.path.join(self.temp.name, "recovery-stage"),
        )
        old_fence = result["locks"][0]["fencing_token"]
        checkpoint = coordinator.advance(
            self.connection, result["job"]["job_id"], 0, old_fence, "SCAN_CREATED"
        )
        checkpoint = coordinator.advance(
            self.connection, result["job"]["job_id"], checkpoint["checkpoint_seq"],
            old_fence, "INFO_STAGED"
        )
        os.remove(self.info_path)

        recovery = ScanImportRecoveryService(coordinator=coordinator)
        validated = recovery.validate(self.connection, result["job"]["job_id"])
        self.assertEqual(
            validated["artifact"]["sha256"], result["artifact"]["sha256"]
        )
        reclaimed = recovery.reclaim(
            self.connection, result["job"]["job_id"],
            os.path.join(self.temp.name, "recovery-stage"),
            owner_token="restarted-owner",
        )
        new_fence = reclaimed["locks"][0]["fencing_token"]
        self.assertGreater(new_fence, old_fence)
        stale_batch = [{
            "repository_name": "repo-a", "file_path": "src/stale.c",
            "file_path_hash": "s" * 32,
            "lines": [{"line_number": 1, "coverage_state": "uncovered"}],
        }]
        with self.assertRaisesRegex(ValueError, "LOCK_FENCING_FAILED"):
            coordinator._ingest_coverage_batch(
                self.connection, result["job"]["job_id"], result["owner_token"],
                old_fence, result["scan"]["id"], stale_batch, 1, 0, 0, {},
                coordinator._project_service(),
            )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM coverage_files WHERE scan_id=?",
                (result["scan"]["id"],),
            ).fetchone()[0], 0
        )
        with self.assertRaises(ValueError) as raised:
            coordinator.advance(
                self.connection, result["job"]["job_id"],
                checkpoint["checkpoint_seq"], old_fence, "COVERAGE_IMPORTED",
            )
        self.assertEqual(str(raised.exception), "STALE_IMPORT_CHECKPOINT")

    def test_recovery_handler_version_mismatch_fails_closed(self):
        coordinator = ScanImportCoordinator()
        result = coordinator.create(
            self.connection, "handler-mismatch", self.info_path,
            repository_resource_ids=[self.resource_id],
            staging_root=os.path.join(self.temp.name, "handler-stage"),
        )
        self.connection.execute(
            "UPDATE coverage_background_jobs SET handler_version=? WHERE job_id=?",
            ("UNSUPPORTED_HANDLER", result["job"]["job_id"]),
        )
        self.connection.commit()
        recovery = ScanImportRecoveryService(coordinator=coordinator)
        with self.assertRaises(ValueError) as raised:
            recovery.validate(self.connection, result["job"]["job_id"])
        self.assertEqual(str(raised.exception), "UNSUPPORTED_SCAN_IMPORT_HANDLER")

    def test_persisted_read_set_blocks_publish_and_releases_candidate(self):
        projects = ProjectRepository()
        states = ProjectStateRepository()
        repositories = RepositoryRepository()
        project_service = ProjectService(
            projects, states, LineIndexRepository(), repository_repo=repositories
        )
        old = project_service.create_scan_and_ingest(
            self.connection, "read-set", [{
                "repository_name": "repo-a", "file_path": "src/a.c",
                "file_path_hash": "r" * 32,
                "lines": [{"line_number": 1, "coverage_state": "uncovered"}],
            }], info_sha256="old-read-set",
        )
        line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE file_id IN "
            "(SELECT id FROM coverage_files WHERE scan_id=?)", (old["id"],)
        ).fetchone()[0]
        domain = AnalysisDomainRepository()
        record = domain.create_record(
            self.connection, {"status": "可覆盖", "coverage_method": "unit"}
        )
        relation = domain.create_link(
            self.connection, old["id"], line_id, record["id"],
            review_state="MANUAL_CONFIRMED",
        )
        self.connection.commit()
        read_set = [{
            "relation_id": relation["id"],
            "relation_revision": relation["relation_revision"],
            "record_id": record["id"],
            "content_revision": record["content_revision"],
        }]

        coordinator = ScanImportCoordinator(
            project_repository=projects, state_repository=states,
            repository_repository=repositories,
        )
        result = coordinator.create(
            self.connection, "read-set", self.info_path,
            repository_resource_ids=[self.resource_id],
            staging_root=os.path.join(self.temp.name, "read-set-stage"),
        )
        checkpoint = result["checkpoint"]
        phases = (
            "SCAN_CREATED", "INFO_STAGED", "COVERAGE_IMPORTED", "GIT_VERIFIED",
            "SOURCE_PREPARED", "LINE_MAP_BUILT", "INHERITANCE_COMPUTED",
            "STATS_REBUILT", "CONSISTENCY_VERIFIED",
        )
        for phase in phases:
            checkpoint = coordinator.advance(
                self.connection, result["job"]["job_id"],
                checkpoint["checkpoint_seq"], result["locks"][0]["fencing_token"],
                phase, payload={"read_set": read_set},
            )
        coordinator.seal(
            self.connection, result["job"]["job_id"], result["owner_token"],
            result["locks"][0]["fencing_token"],
        )

        domain.update_record(
            self.connection, record["id"],
            {"status": "无法覆盖", "coverage_method": "updated"},
            expected_revision=record["content_revision"],
        )
        self.connection.commit()
        with self.assertRaises(ValueError) as raised:
            coordinator.publish(self.connection, result["job"]["job_id"])
        self.assertEqual(str(raised.exception), "READ_SET_CHANGED")
        self.assertEqual(
            self.connection.execute(
                "SELECT current_scan_id FROM coverage_project_state WHERE project_id=?",
                (old["project_id"],),
            ).fetchone()[0], old["id"]
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM coverage_scans WHERE id=?",
                (result["scan"]["id"],),
            ).fetchone()[0], "ABORTED"
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT state FROM coverage_background_jobs WHERE job_id=?",
                (result["job"]["job_id"],),
            ).fetchone()[0], "failed"
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM coverage_repository_resource_locks"
            ).fetchone()[0], 0
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT phase, error_class FROM coverage_import_failures WHERE job_id=?",
                (result["job"]["job_id"],),
            ).fetchone()[0:2], ("PUBLISH", "ValueError")
        )
        failure_key = self.connection.execute(
            "SELECT failure_key_hash FROM coverage_import_failures WHERE job_id=?",
            (result["job"]["job_id"],),
        ).fetchone()[0]
        self.assertEqual(len(failure_key), 64)

    def test_enqueue_failure_aborts_candidate_and_releases_lock(self):
        coordinator = ScanImportCoordinator()
        result = coordinator.create(
            self.connection, "enqueue-failure", self.info_path,
            repository_resource_ids=[self.resource_id],
            staging_root=os.path.join(self.temp.name, "enqueue-stage"),
        )
        recovery = ScanImportRecoveryService(coordinator=coordinator)
        recovery.record_failure(
            self.connection, result["job"]["job_id"], "ENQUEUE",
            "RuntimeError", "queue is full",
            fencing_token=result["locks"][0]["fencing_token"],
            scan_status="ABORTED",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM coverage_scans WHERE id=?",
                (result["scan"]["id"],),
            ).fetchone()[0], "ABORTED"
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT state, error_message FROM coverage_background_jobs WHERE job_id=?",
                (result["job"]["job_id"],),
            ).fetchone()[0:2], ("failed", "queue is full")
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM coverage_repository_resource_locks"
            ).fetchone()[0], 0
        )

    def test_repository_master_rename_alias_and_retire_preserve_identity(self):
        projects = ProjectRepository()
        project = projects.ensure_project(self.connection, "repository-master")
        repositories = RepositoryRepository()
        original = repositories.ensure(
            self.connection, project["id"], "repo-a", canonical_remote="origin-a"
        )
        renamed = repositories.rename(
            self.connection, project["id"], original["id"], "repo-b"
        )
        self.assertEqual(renamed["id"], original["id"])
        self.assertEqual(
            repositories.get_by_name(self.connection, project["id"], "repo-a")["id"],
            original["id"],
        )
        repositories.add_alias(self.connection, project["id"], original["id"], "legacy-a")
        self.assertEqual(
            repositories.get_by_name(self.connection, project["id"], "legacy-a")["id"],
            original["id"],
        )
        other = repositories.ensure(self.connection, project["id"], "repo-c")
        with self.assertRaises(ValueError):
            repositories.add_alias(self.connection, project["id"], other["id"], "repo-a")
        repositories.retire(self.connection, original["id"])
        self.assertIsNone(
            repositories.get_by_name(self.connection, project["id"], "legacy-a")
        )
        with self.assertRaises(ValueError) as raised:
            repositories.ensure(self.connection, project["id"], "repo-b")
        self.assertEqual(str(raised.exception), "REPOSITORY_RETIRED")

    def test_publication_compare_and_swap_distinguishes_null_current(self):
        projects = ProjectRepository()
        project = projects.ensure_project(self.connection, "publication-cas")
        states = ProjectStateRepository()
        states.ensure(self.connection, project["id"], current_scan_id=None)
        first = projects.create_scan(
            self.connection, project["id"], "first", "coverage", "full",
            info_sha256="hash-first",
        )
        second = projects.create_scan(
            self.connection, project["id"], "second", "coverage", "full",
            info_sha256="hash-second",
        )
        projects.seal_scan(self.connection, first["id"])
        projects.seal_scan(self.connection, second["id"])
        publication = ScanPublicationService()
        publication.publish_in_transaction(
            self.connection, project["id"], first["id"],
            expected_current_scan_id=None,
        )
        with self.assertRaises(ValueError) as raised:
            publication.publish_in_transaction(
                self.connection, project["id"], second["id"],
                expected_current_scan_id=None,
            )
        self.assertEqual(str(raised.exception), "CURRENT_POINTER_CHANGED")

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

    def test_restart_resumes_after_durable_coverage_checkpoint(self):
        projects = ProjectRepository()
        states = ProjectStateRepository()
        repositories = RepositoryRepository()
        project_service = ProjectService(
            projects, states, LineIndexRepository(), repository_repo=repositories
        )
        domain_repository = AnalysisDomainRepository()
        dependencies = dict(
            project_repository=projects, state_repository=states,
            repository_repository=repositories, project_service=project_service,
            import_service=ScanImportService(project_service),
            inheritance_engine=InheritanceEngine(domain_repository=domain_repository),
            file_state_repository=FileStateRepository(),
            analysis_domain_service=AnalysisDomainService(domain_repository),
        )

        class StopAfterCoverageCoordinator(ScanImportCoordinator):
            def __init__(self, *args, **kwargs):
                self._stopped = False
                super().__init__(*args, **kwargs)

            def _run_phase_operation(self, connection, job_id, owner_token,
                                     fencing_token, target, payload=None,
                                     operation=None):
                result = super()._run_phase_operation(
                    connection, job_id, owner_token, fencing_token, target,
                    payload=payload, operation=operation,
                )
                if target == "COVERAGE_IMPORTED" and not self._stopped:
                    self._stopped = True
                    raise RuntimeError("simulated process stop")
                return result

        first = StopAfterCoverageCoordinator(**dependencies)
        result = first.create(
            self.connection, "restartable", self.info_path,
            repository_resource_ids=[self.resource_id],
            staging_root=os.path.join(self.temp.name, "restart-stage"),
        )
        with self.assertRaisesRegex(RuntimeError, "simulated process stop"):
            first.execute(
                self.connection, result["job"]["job_id"],
                owner_token=result["owner_token"],
                fencing_token=result["locks"][0]["fencing_token"],
            )
        self.assertEqual(
            first.checkpoints.get(self.connection, result["job"]["job_id"])["phase"],
            "COVERAGE_IMPORTED",
        )
        self.assertIsNone(self.connection.execute(
            "SELECT current_scan_id FROM coverage_project_state"
        ).fetchone()[0])

        restarted = ScanImportCoordinator(**dependencies)
        state = restarted.execute(
            self.connection, result["job"]["job_id"],
            owner_token=result["owner_token"],
            fencing_token=result["locks"][0]["fencing_token"],
        )
        self.assertEqual(state["current_scan_id"], result["scan"]["id"])
        self.assertEqual(self.connection.execute(
            "SELECT COUNT(*) FROM coverage_lines l JOIN coverage_files f "
            "ON f.id=l.file_id WHERE f.scan_id=?", (result["scan"]["id"],)
        ).fetchone()[0], 1)
        self.assertEqual(
            restarted.checkpoints.get(self.connection, result["job"]["job_id"])["phase"],
            "PUBLISHED",
        )

    def test_coverage_batches_commit_checkpoint_atomically_and_resume(self):
        projects = ProjectRepository()
        states = ProjectStateRepository()
        repositories = RepositoryRepository()
        project_service = ProjectService(
            projects, states, LineIndexRepository(), repository_repo=repositories
        )
        domain_repository = AnalysisDomainRepository()
        dependencies = dict(
            project_repository=projects, state_repository=states,
            repository_repository=repositories, project_service=project_service,
            import_service=ScanImportService(project_service),
            inheritance_engine=InheritanceEngine(domain_repository=domain_repository),
            file_state_repository=FileStateRepository(),
            analysis_domain_service=AnalysisDomainService(domain_repository),
        )
        info_path = os.path.join(self.temp.name, "three-files.info")
        with open(info_path, "w") as stream:
            stream.write(
                "TN:\nSF:src/one.c\nDA:1,0\nend_of_record\n"
                "TN:\nSF:src/two.c\nDA:2,0\nend_of_record\n"
                "TN:\nSF:src/three.c\nDA:3,0\nend_of_record\n"
            )

        class StopAfterFirstBatch(ScanImportCoordinator):
            COVERAGE_IMPORT_FILE_BATCH_SIZE = 1

            def __init__(self, *args, **kwargs):
                self.stopped = False
                super().__init__(*args, **kwargs)

            def _ingest_coverage_batch(self, *args, **kwargs):
                result = super()._ingest_coverage_batch(*args, **kwargs)
                batch_seq = int(args[6])
                if batch_seq == 1 and not self.stopped:
                    self.stopped = True
                    raise RuntimeError("simulated process stop after committed batch")
                return result

        first = StopAfterFirstBatch(**dependencies)
        created = first.create(
            self.connection, "batched-restart", info_path,
            repository_resource_ids=[self.resource_id],
            staging_root=os.path.join(self.temp.name, "batched-stage"),
        )
        with self.assertRaisesRegex(RuntimeError, "committed batch"):
            first.execute(
                self.connection, created["job"]["job_id"],
                owner_token=created["owner_token"],
                fencing_token=created["locks"][0]["fencing_token"],
            )
        checkpoint = first.checkpoints.get(
            self.connection, created["job"]["job_id"]
        )
        payload = checkpoint["payload"]
        if isinstance(payload, str):
            import json
            payload = json.loads(payload)
        self.assertEqual(checkpoint["phase"], "INFO_STAGED")
        self.assertEqual(payload["batch_seq"], 1)
        self.assertEqual(payload["processed_file_count"], 1)
        self.assertEqual(payload["processed_line_count"], 1)
        self.assertEqual(payload["scan_id"], created["scan"]["id"])
        self.assertEqual(payload["artifact_sha256"], created["artifact"]["sha256"])
        self.assertEqual(payload["fencing_token"], created["locks"][0]["fencing_token"])
        self.assertEqual(payload["last_file_identity"]["file_path"], "src/one.c")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM coverage_files WHERE scan_id=?",
                (created["scan"]["id"],),
            ).fetchone()[0], 1
        )

        restarted = ScanImportCoordinator(**dependencies)
        restarted.COVERAGE_IMPORT_FILE_BATCH_SIZE = 1
        state = restarted.execute(
            self.connection, created["job"]["job_id"],
            owner_token=created["owner_token"],
            fencing_token=created["locks"][0]["fencing_token"],
        )
        self.assertEqual(state["current_scan_id"], created["scan"]["id"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM coverage_files WHERE scan_id=?",
                (created["scan"]["id"],),
            ).fetchone()[0], 3
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM coverage_lines l JOIN coverage_files f "
                "ON f.id=l.file_id WHERE f.scan_id=?",
                (created["scan"]["id"],),
            ).fetchone()[0], 3
        )
        self.assertEqual(
            restarted.checkpoints.get(
                self.connection, created["job"]["job_id"]
            )["phase"], "PUBLISHED"
        )

    def test_last_coverage_batch_failure_does_not_advance_phase(self):
        projects = ProjectRepository()
        states = ProjectStateRepository()
        repositories = RepositoryRepository()
        project_service = ProjectService(
            projects, states, LineIndexRepository(), repository_repo=repositories
        )
        domain_repository = AnalysisDomainRepository()
        dependencies = dict(
            project_repository=projects, state_repository=states,
            repository_repository=repositories, project_service=project_service,
            import_service=ScanImportService(project_service),
            inheritance_engine=InheritanceEngine(domain_repository=domain_repository),
            file_state_repository=FileStateRepository(),
            analysis_domain_service=AnalysisDomainService(domain_repository),
        )
        info_path = os.path.join(self.temp.name, "last-batch-failure.info")
        with open(info_path, "w") as stream:
            stream.write(
                "TN:\nSF:src/first.c\nDA:1,0\nend_of_record\n"
                "TN:\nSF:src/last.c\nDA:2,0\nend_of_record\n"
            )

        class FailLastBatch(ScanImportCoordinator):
            COVERAGE_IMPORT_FILE_BATCH_SIZE = 1

            def _ingest_coverage_batch(self, *args, **kwargs):
                if int(args[6]) == 2:
                    raise RuntimeError("simulated last batch failure")
                return super()._ingest_coverage_batch(*args, **kwargs)

        coordinator = FailLastBatch(**dependencies)
        created = coordinator.create(
            self.connection, "last-batch-failure", info_path,
            repository_resource_ids=[self.resource_id],
            staging_root=os.path.join(self.temp.name, "last-batch-stage"),
        )
        with self.assertRaisesRegex(RuntimeError, "last batch failure"):
            coordinator.execute(
                self.connection, created["job"]["job_id"],
                owner_token=created["owner_token"],
                fencing_token=created["locks"][0]["fencing_token"],
            )
        checkpoint = coordinator.checkpoints.get(
            self.connection, created["job"]["job_id"]
        )
        self.assertEqual(checkpoint["phase"], "INFO_STAGED")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM coverage_files WHERE scan_id=?",
                (created["scan"]["id"],),
            ).fetchone()[0], 1
        )


if __name__ == "__main__":
    unittest.main()
