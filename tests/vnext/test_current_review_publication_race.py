import sqlite3
import unittest
from unittest import mock

from app.db.repositories.project_state_repository import ProjectStateRepository
from app.scan_import.publication import ScanPublicationService
from app.services.analysis_service import AnalysisService
from scripts.upgrade.migration_runner import create_sqlite_schema


class _RecordingStateRepository(ProjectStateRepository):
    def __init__(self):
        self.lock_calls = []

    def lock_for_update(self, connection, project_id):
        self.lock_calls.append(int(project_id))
        return super().lock_for_update(connection, project_id)


class CurrentReviewPublicationRaceTest(unittest.TestCase):
    def test_project_state_lock_uses_for_update_on_mysql_connections(self):
        repository = ProjectStateRepository()
        fake_connection = object()
        fetch_state = mock.patch(
            "app.db.repositories.project_state_repository.fetchone",
            side_effect=[
                {"project_id": 7}, {"project_id": 7},
            ],
        )
        with mock.patch(
                "app.db.repositories.project_state_repository.is_sqlite",
                return_value=False), \
                mock.patch(
                    "app.db.repositories.project_state_repository.execute"
                ) as execute, fetch_state as fetchone:
            execute.return_value.close = mock.Mock()
            repository.lock_for_update(fake_connection, 7)
        self.assertIn(
            "FOR UPDATE", fetchone.call_args_list[-1].args[1].upper()
        )

    def test_analysis_and_publication_share_project_state_lock_boundary(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        create_sqlite_schema(connection)
        from app.db.repositories import (
            LineIndexRepository, ProjectRepository,
        )
        projects = ProjectRepository()
        states = _RecordingStateRepository()
        project = projects.ensure_project(connection, "current-lock")
        scan = projects.create_scan(
            connection, project["id"], "current-lock-scan", "import", "full",
            status="building",
        )
        file_row = projects.ensure_file(
            connection, scan["id"], "", "l" * 32, "src/a.c", "a.c"
        )
        LineIndexRepository().upsert_lines(connection, file_row["id"], [{
            "line_number": 1, "line_text": "return 0;",
            "coverage_state": "uncovered",
        }])
        projects.seal_scan(connection, scan["id"])
        states.ensure(connection, project["id"], current_scan_id=scan["id"])
        AnalysisService(state_repo=states).save(
            connection, "current-lock", scan["id"], [{
                "file_path_hash": "l" * 32, "line_number": 1,
                "status": "未确认",
            }], enforce_current=True,
        )
        ScanPublicationService(states).publish_in_transaction(
            connection, project["id"], scan["id"],
        )
        self.assertGreaterEqual(states.lock_calls.count(project["id"]), 2)


if __name__ == "__main__":
    unittest.main()
