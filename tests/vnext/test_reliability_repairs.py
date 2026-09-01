import sqlite3
import unittest
from unittest import mock

from app.db.retry import is_retryable_deadlock, run_transaction_with_deadlock_retry
from app.db.repositories import (
    FileStateRepository, LineIndexRepository, ProjectRepository,
    ProjectStateRepository,
)
from app.services.file_state_service import (
    FileStateReadyGateError, FileStateService,
)
from app.services.progress_service import ProgressService
from app.scan_import.coordinator import ScanImportCoordinator
from scripts.diagnostics.project_identity_collision import find_collisions
from scripts.upgrade.migration_runner import create_sqlite_schema


class _Deadlock(Exception):
    pass


class ReliabilityRepairsTest(unittest.TestCase):
    def test_deadlock_retry_replays_one_transaction_with_exponential_backoff(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE values_table (value INTEGER)")
        attempts = []
        delays = []

        def operation(conn):
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise _Deadlock(1213, "deadlock")
            conn.execute("INSERT INTO values_table(value) VALUES (1)")
            return "ok"

        self.assertTrue(is_retryable_deadlock(_Deadlock(1213, "deadlock")))
        self.assertEqual(
            run_transaction_with_deadlock_retry(
                connection, operation, max_retries=2, base_delay=0.01,
                sleep=delays.append,
            ),
            "ok",
        )
        self.assertEqual(attempts, [1, 2, 3])
        self.assertEqual(delays, [0.01, 0.02])
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM values_table"
        ).fetchone()[0], 1)

    def test_import_batch_order_is_stable_and_project_name_collision_is_diagnostic(self):
        files = [
            {"repository_name": "repo", "file_path_hash": "b", "file_path": "z.c"},
            {"repository_name": "repo", "file_path_hash": "a", "file_path": "a.c"},
        ]
        batches = list(ScanImportCoordinator._iter_coverage_batches(
            files, max_files=10, max_lines=0, max_est_bytes=0
        ))
        self.assertEqual(
            [item["file_path_hash"] for item in batches[0]], ["a", "b"]
        )
        collisions = find_collisions(["FOSV6R2", "FOS_V6R2"])
        self.assertEqual(collisions[0]["exact_project_names"], ["FOSV6R2", "FOS_V6R2"])


class ProgressReadyGateTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_sqlite_schema(self.connection)
        self.projects = ProjectRepository()
        self.states = ProjectStateRepository()
        self.file_states = FileStateRepository()
        self.lines = LineIndexRepository()
        self.project = self.projects.ensure_project(self.connection, "ready-gate")
        self.scan = self.projects.create_scan(
            self.connection, self.project["id"], "ready-gate-scan", "import", "full"
        )
        self.file = self.projects.ensure_file(
            self.connection, self.scan["id"], "repo", "f" * 32,
            "src/ready-gate.c", "ready-gate.c",
        )
        self.lines.upsert_lines(self.connection, self.file["id"], [{
            "line_number": 1, "line_text": "return 0;",
            "coverage_state": "uncovered",
        }])
        self.states.ensure(
            self.connection, self.project["id"],
            current_scan_id=self.scan["id"],
        )
        self.states.advance(self.connection, self.project["id"])
        self.connection.commit()
        self.progress = ProgressService(
            file_state_repo=self.file_states,
            project_repo=self.projects,
            state_repo=self.states,
        )

    def tearDown(self):
        self.connection.close()

    def _seed_false_ready_projection(self):
        self.file_states.rebuild_scan(
            self.connection, self.scan["id"], 1, None
        )
        self.connection.execute(
            "UPDATE coverage_file_state SET pending_total=0 "
            "WHERE scan_id=? AND file_id=?",
            (self.scan["id"], self.file["id"]),
        )
        self.states.mark_ready(self.connection, self.project["id"], 1)
        self.connection.commit()

    def test_false_ready_uses_authoritative_facts_for_summary_and_pages(self):
        self._seed_false_ready_projection()

        summary = self.progress.summary(
            self.connection, "ready-gate", self.scan["id"]
        )
        self.assertEqual(summary["source"], "authoritative")
        self.assertEqual(summary["derived_state_status"], "INVALID")
        self.assertEqual(summary["pending_total"], 1)

        files = self.progress.files_page(
            self.connection, "ready-gate", self.scan["id"]
        )
        self.assertEqual(files["rows"][0]["pending_total"], 1)
        pending_files = self.progress.pending_by_file(
            self.connection, "ready-gate", self.scan["id"]
        )
        self.assertEqual(pending_files["rows"][0]["unanalyzed"], 1)
        pending_lines = self.progress.pending_page(
            self.connection, "ready-gate", self.scan["id"]
        )
        self.assertEqual(pending_lines["total"], 1)

    def test_online_validation_exception_fails_safe_to_authoritative_facts(self):
        self._seed_false_ready_projection()
        with mock.patch.object(
                self.progress.file_state_service,
                "validate_rebuilt",
                side_effect=ValueError("malformed derived row"),
        ):
            summary = self.progress.summary(
                self.connection, "ready-gate", self.scan["id"]
            )
            self.assertEqual(summary["source"], "authoritative")
            self.assertEqual(summary["derived_state_status"], "INVALID")
            self.assertEqual(
                summary["derived_state_reason"], "FILE_STATE_VALIDATION_ERROR"
            )
            self.assertEqual(summary["pending_conservation"]["status"], "FAILED")
            self.assertEqual(summary["pending_total"], 1)

            files = self.progress.files_page(
                self.connection, "ready-gate", self.scan["id"]
            )
            self.assertEqual(files["rows"][0]["pending_total"], 1)
            pending_files = self.progress.pending_by_file(
                self.connection, "ready-gate", self.scan["id"]
            )
            self.assertEqual(pending_files["rows"][0]["unanalyzed"], 1)
            pending_lines = self.progress.pending_page(
                self.connection, "ready-gate", self.scan["id"]
            )
            self.assertEqual(pending_lines["total"], 1)

    def test_zero_data_version_ready_projection_is_usable(self):
        self.connection.execute(
            "UPDATE coverage_project_state SET data_version=0, "
            "file_state_version=0 WHERE project_id=?",
            (self.project["id"],),
        )
        self.file_states.rebuild_scan(self.connection, self.scan["id"], 0, None)
        self.states.mark_ready(self.connection, self.project["id"], 0)
        self.connection.commit()

        summary = self.progress.summary(
            self.connection, "ready-gate", self.scan["id"]
        )
        self.assertEqual(summary["source"], "coverage_file_state")
        self.assertEqual(summary["derived_state_status"], "READY")
        self.assertEqual(summary["data_version"], 0)
        self.assertEqual(summary["file_state_version"], 0)

    def test_failed_rebuild_persists_stale_marker_after_gate_error(self):
        self._seed_false_ready_projection()
        with mock.patch.object(
                self.progress.file_state_service.file_states,
                "rebuild_scan",
                side_effect=FileStateReadyGateError({"reason": "forced"}),
        ):
            with self.assertRaises(FileStateReadyGateError):
                self.progress.rebuild(
                    self.connection, "ready-gate", self.scan["id"]
                )
        state = self.states.get(self.connection, self.project["id"])
        self.assertEqual(int(state["file_state_version"]), 0)

    def test_failed_rebuild_persists_stale_marker_after_repository_error(self):
        self._seed_false_ready_projection()
        with mock.patch.object(
                self.progress.file_state_service.file_states,
                "rebuild_scan",
                side_effect=RuntimeError("repository unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "repository unavailable"):
                self.progress.rebuild(
                    self.connection, "ready-gate", self.scan["id"]
                )
        state = self.states.get(self.connection, self.project["id"])
        self.assertEqual(int(state["file_state_version"]), 0)

    def test_confirmed_legacy_fact_is_not_counted_as_ordinary_pending(self):
        line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE file_id=? AND line_number=?",
            (self.file["id"], 1),
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO coverage_analyses("
            "line_id, status, is_draft, reviewer, coverage_method, "
            "uncovered_reason, comment, created_at, updated_at) "
            "VALUES (?, '可覆盖', 0, 'legacy', 'unit', '', '', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (line_id,),
        )
        self.file_states.rebuild_scan(
            self.connection, self.scan["id"], 1, None
        )
        aggregate = self.file_states.scan_aggregate(
            self.connection, self.scan["id"]
        )
        self.assertEqual(int(aggregate["pending_total"]), 0)
        self.assertEqual(int(aggregate["ordinary_pending_total"]), 0)
        self.assertEqual(
            self.file_states.pending_conservation(
                self.connection, self.scan["id"]
            )["status"],
            "PASSED",
        )

    def test_ready_publication_rejects_concurrent_version_change(self):
        service = FileStateService(
            file_state_repo=self.file_states,
            state_repo=self.states,
        )
        with mock.patch.object(
                service.states, "mark_ready",
                return_value={
                    "project_id": self.project["id"],
                    "data_version": 2,
                    "file_state_version": 0,
                }), self.assertRaises(FileStateReadyGateError) as raised:
            service.rebuild_validate_and_mark_ready(
                self.connection, self.project["id"], self.scan["id"], 1
            )
        self.assertEqual(raised.exception.gate["reason"], "DATA_VERSION_CHANGED")
        state = self.states.get(self.connection, self.project["id"])
        self.assertEqual(int(state["file_state_version"]), 0)

    def test_partial_rebuild_rebases_unchanged_files_and_keeps_full_ready_gate(self):
        other_file = self.projects.ensure_file(
            self.connection, self.scan["id"], "repo", "g" * 32,
            "src/other-ready-gate.c", "other-ready-gate.c",
        )
        self.lines.upsert_lines(self.connection, other_file["id"], [{
            "line_number": 1, "line_text": "return 1;",
            "coverage_state": "uncovered",
        }])
        self.file_states.rebuild_scan(
            self.connection, self.scan["id"], 1, None
        )
        self.states.mark_ready(self.connection, self.project["id"], 1)
        self.connection.commit()

        self.connection.execute(
            "UPDATE coverage_lines SET coverage_state='covered' "
            "WHERE file_id=? AND line_number=1",
            (self.file["id"],),
        )
        next_state = self.states.advance(self.connection, self.project["id"])
        service = FileStateService(
            file_state_repo=self.file_states,
            state_repo=self.states,
        )
        with mock.patch.object(
                self.file_states, "rebase_scan_version",
                wraps=self.file_states.rebase_scan_version) as rebase, \
                mock.patch.object(
                    self.file_states, "rebuild_scan",
                    wraps=self.file_states.rebuild_scan) as rebuild:
            ready = service.rebuild_validate_and_mark_ready_in_transaction(
                self.connection, self.project["id"], self.scan["id"],
                int(next_state["data_version"]),
                affected_file_ids=[self.file["id"]],
            )
        self.assertEqual(int(ready["file_state_version"]), 2)
        self.assertEqual(rebase.call_count, 1)
        args, kwargs = rebuild.call_args
        self.assertEqual(
            kwargs["file_ids"], [self.file["id"]]
        )
        first_state = self.file_states.get(
            self.connection, self.scan["id"], self.file["id"]
        )
        other_state = self.file_states.get(
            self.connection, self.scan["id"], other_file["id"]
        )
        self.assertEqual(int(first_state["total_uncovered"]), 0)
        self.assertEqual(int(first_state["pending_total"]), 0)
        self.assertEqual(int(other_state["total_uncovered"]), 1)
        self.assertEqual(int(other_state["pending_total"]), 1)
        self.assertEqual(int(other_state["data_version"]), 2)


if __name__ == "__main__":
    unittest.main()
