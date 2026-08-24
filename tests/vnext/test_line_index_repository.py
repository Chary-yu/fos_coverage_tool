import sqlite3
import unittest
from unittest import mock

from app.db.repositories import LineIndexRepository, ProjectRepository, ProjectStateRepository
from app.services.project_service import ProjectService
from scripts.upgrade.migration_runner import create_sqlite_schema


class LineIndexRepositoryTest(unittest.TestCase):
    def test_return_rows_uses_bounded_bulk_readback(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        create_sqlite_schema(connection)
        projects = ProjectRepository()
        line_repo = LineIndexRepository()
        service = ProjectService(projects, ProjectStateRepository(), line_repo)
        scan = service.create_scan(
            connection, "bulk-readback", info_sha256="bulk-readback-fixture"
        )
        file_row = projects.ensure_files(connection, scan["id"], [{
            "repository_name": "repo-a", "file_path_hash": "r" * 32,
            "file_path": "src/bulk.c", "source_file_name": "bulk.c",
        }])[("repo-a", "r" * 32)]
        records = [{"line_number": number, "coverage_state": "uncovered"}
                   for number in range(1, 1002)]
        with mock.patch(
                "app.db.repositories.line_index_repository.fetchone",
                wraps=__import__(
                    "app.db.repositories.line_index_repository",
                    fromlist=["fetchone"],
                ).fetchone,
        ) as fetchone:
            rows = line_repo.upsert_lines(
                connection, file_row["id"], records, return_rows=True
            )
        self.assertEqual(len(rows), len(records))
        self.assertEqual([row["line_number"] for row in rows], list(range(1, 1002)))
        # One scan-state assertion is expected; row identities are resolved by
        # bounded IN queries rather than one SELECT per inserted line.
        self.assertLessEqual(fetchone.call_count, 2)


if __name__ == "__main__":
    unittest.main()
