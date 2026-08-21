import os
import sqlite3
import tempfile
import unittest

from app.services.analysis_domain_service import AnalysisDomainService
from app.services.analysis_service import AnalysisService
from scripts.upgrade.domain_migration import apply_analysis_domain
from scripts.upgrade.migration_runner import create_sqlite_schema
from app.db.repositories import ProjectRepository, LineIndexRepository


class AnalysisDomainTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_sqlite_schema(self.connection)
        self.projects = ProjectRepository()
        self.lines = LineIndexRepository()
        project = self.projects.ensure_project(self.connection, "domain")
        self.scan = self.projects.create_scan(
            self.connection, project["id"], "scan-domain", "import", "full",
            status="building",
        )
        file_row = self.projects.ensure_file(
            self.connection, self.scan["id"], "", "f" * 32, "src/a.c", "a.c"
        )
        self.lines.upsert_lines(self.connection, file_row["id"], [
            {"line_number": 10, "line_text": "return 0;", "coverage_state": "uncovered"},
            {"line_number": 11, "line_text": "return 1;", "coverage_state": "uncovered"},
        ])
        self.projects.seal_scan(self.connection, self.scan["id"])
        self.file_id = file_row["id"]

    def tearDown(self):
        self.connection.close()

    def test_manual_save_creates_record_exact_block_and_one_current_link_per_line(self):
        result = AnalysisService().save(
            self.connection, "domain", self.scan["id"],
            [{"file_path_hash": "f" * 32, "line_start": 10, "line_end": 11,
              "status": "可覆盖", "coverage_method": "unit", "reviewer": "alice"}],
        )
        self.assertEqual(result["saved"], 2)
        records = self.connection.execute(
            "SELECT * FROM coverage_analysis_records"
        ).fetchall()
        blocks = self.connection.execute(
            "SELECT start_line, end_line, block_identity_verified FROM coverage_analysis_blocks"
        ).fetchall()
        links = self.connection.execute(
            "SELECT review_state, reviewed_by FROM coverage_analysis_line_links ORDER BY line_id"
        ).fetchall()
        self.assertEqual(len(records), 1)
        self.assertEqual([tuple(row) for row in blocks], [(10, 11, 1)])
        self.assertEqual([tuple(row) for row in links],
                         [("MANUAL_CONFIRMED", "alice"), ("MANUAL_CONFIRMED", "alice")])
        self.assertEqual(
            self.connection.execute(
                "SELECT current_scan_id FROM coverage_project_state"
            ).fetchone()[0], None,
            "analysis save must not publish a Scan",
        )

    def test_backfill_is_idempotent_and_does_not_fabricate_verified_block(self):
        self.connection.execute("""
            INSERT INTO coverage_analyses(
                line_id, status, is_draft, reviewer, coverage_method,
                uncovered_reason, comment, created_at, updated_at
            ) VALUES (?, '可覆盖', 0, 'legacy', 'unit', '', 'old',
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (self.connection.execute(
            "SELECT id FROM coverage_lines WHERE line_number=10"
        ).fetchone()[0],))
        self.connection.commit()
        result = apply_analysis_domain(self.connection)
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["backfill"]["created"], 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM coverage_analysis_blocks"
            ).fetchone()[0], 0
        )
        self.assertEqual(apply_analysis_domain(self.connection)["backfill"]["created"], 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT schema_version FROM coverage_schema_meta "
                "WHERE schema_key='coverage_analysis_domain'"
            ).fetchone()[0], 1
        )

    def test_consistency_audit_detects_cross_scan_link(self):
        service = AnalysisDomainService()
        record = service.repository.create_record(
            self.connection, {"status": "未确认"}, origin="MANUAL"
        )
        line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE line_number=10"
        ).fetchone()[0]
        service.repository.create_link(
            self.connection, self.scan["id"], line_id, record["id"]
        )
        # The unique key prevents a second current relation, but an incorrect
        # scan_id on the same line is still a detectable cross-scan invariant.
        self.connection.execute(
            "UPDATE coverage_analysis_line_links SET scan_id=?", (999,)
        )
        self.connection.commit()
        self.assertEqual(service.audit_consistency(self.connection)["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
