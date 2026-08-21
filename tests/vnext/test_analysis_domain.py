import os
import sqlite3
import tempfile
import unittest

from app.services.analysis_domain_service import AnalysisDomainService
from app.services.analysis_service import AnalysisService
from app.inheritance.rejections import InheritanceRejectionService
from app.db.repositories.analysis_domain_repository import INHERITED_PENDING
from scripts.upgrade.domain_migration import apply_analysis_domain
from scripts.upgrade.migration_runner import create_sqlite_schema
from app.db.repositories import ProjectRepository, LineIndexRepository, ProjectStateRepository
from app.services.progress_service import ProgressService


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
            ).fetchone()[0], 2
        )
        constraint_migration = self.connection.execute(
            "SELECT state, to_version FROM coverage_schema_migrations "
            "WHERE migration_id='coverage-analysis-domain-constraints-v1'"
        ).fetchone()
        self.assertEqual(tuple(constraint_migration), ("APPLIED", 2))

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

    def test_shared_record_partial_edit_splits_only_selected_line(self):
        service = AnalysisService()
        service.save(
            self.connection, "domain", self.scan["id"],
            [{"file_path_hash": "f" * 32, "line_start": 10, "line_end": 11,
              "status": "可覆盖", "coverage_method": "unit", "reviewer": "alice"}],
        )
        line10, line11 = [
            row[0] for row in self.connection.execute(
                "SELECT id FROM coverage_lines ORDER BY line_number"
            ).fetchall()
        ]
        original = self.connection.execute(
            "SELECT analysis_record_id FROM coverage_analysis_line_links "
            "WHERE line_id=?", (line10,)
        ).fetchone()[0]
        service.save(
            self.connection, "domain", self.scan["id"],
            [{"line_id": line10, "record_id": original,
              "expected_record_revision": 1, "status": "无法覆盖",
              "uncovered_reason": "runtime", "reviewer": "bob"}],
        )
        links = {
            row[0]: row[1] for row in self.connection.execute(
                "SELECT line_id, analysis_record_id "
                "FROM coverage_analysis_line_links ORDER BY line_id"
            ).fetchall()
        }
        self.assertNotEqual(links[line10], original)
        self.assertEqual(links[line11], original)
        self.assertEqual(
            self.connection.execute(
                "SELECT content_revision FROM coverage_analysis_records WHERE id=?",
                (original,),
            ).fetchone()[0],
            1,
        )

    def test_manual_save_relation_revision_is_atomic_compare_and_set(self):
        service = AnalysisService()
        service.save(
            self.connection, "domain", self.scan["id"],
            [{"file_path_hash": "f" * 32, "line_number": 10,
              "status": "可覆盖", "coverage_method": "unit"}],
        )
        line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE line_number=10"
        ).fetchone()[0]
        self.assertEqual(
            self.connection.execute(
                "SELECT relation_revision FROM coverage_analysis_line_links "
                "WHERE line_id=?", (line_id,)
            ).fetchone()[0],
            1,
        )
        service.save(
            self.connection, "domain", self.scan["id"],
            [{"line_id": line_id, "expected_relation_revision": 1,
              "status": "无法覆盖", "uncovered_reason": "changed"}],
        )
        current = self.connection.execute(
            "SELECT relation_revision, review_state FROM coverage_analysis_line_links "
            "WHERE line_id=?", (line_id,)
        ).fetchone()
        self.assertEqual(tuple(current), (2, "MANUAL_CONFIRMED"))
        with self.assertRaises(ValueError) as raised:
            service.save(
                self.connection, "domain", self.scan["id"],
                [{"line_id": line_id, "expected_relation_revision": 1,
                  "status": "可覆盖", "coverage_method": "stale"}],
            )
        self.assertEqual(str(raised.exception), "STALE_RELATION_REVISION")
        self.assertEqual(
            self.connection.execute(
                "SELECT relation_revision FROM coverage_analysis_line_links "
                "WHERE line_id=?", (line_id,)
            ).fetchone()[0],
            2,
        )

    def test_reject_and_undo_preserve_lineage_without_active_overlay(self):
        project_id = self.connection.execute(
            "SELECT project_id FROM coverage_scans WHERE id=?", (self.scan["id"],)
        ).fetchone()[0]
        ProjectStateRepository().ensure(
            self.connection, project_id, current_scan_id=self.scan["id"]
        )
        self.connection.execute(
            "UPDATE coverage_project_state SET current_scan_id=? WHERE project_id=?",
            (self.scan["id"], project_id),
        )
        record = AnalysisDomainService().repository.create_record(
            self.connection, {"status": "可覆盖", "coverage_method": "unit"},
            origin="INHERITED",
        )
        source_record = AnalysisDomainService().repository.create_record(
            self.connection, {"status": "可覆盖"}, origin="MANUAL"
        )
        source_line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE line_number=11"
        ).fetchone()[0]
        source_link = AnalysisDomainService().repository.create_link(
            self.connection, self.scan["id"], source_line_id, source_record["id"],
            review_state="MANUAL_CONFIRMED", relation_origin="MANUAL",
        )
        line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE line_number=10"
        ).fetchone()[0]
        link = AnalysisDomainService().repository.create_link(
            self.connection, self.scan["id"], line_id, record["id"],
            review_state=INHERITED_PENDING, relation_origin="INHERITANCE",
            source_scan_id=self.scan["id"], source_line_id=source_line_id,
            source_relation_id=source_link["id"],
        )
        self.connection.commit()

        rejections = InheritanceRejectionService()
        rejection = rejections.reject(
            self.connection, project_id, self.scan["id"], line_id, "alice",
            int(link["relation_revision"]),
        )
        self.connection.commit()
        rejected_link = self.connection.execute(
            "SELECT is_active, relation_revision FROM coverage_analysis_line_links "
            "WHERE id=?", (link["id"],)
        ).fetchone()
        self.assertEqual(tuple(rejected_link), (0, 2))
        overlay = AnalysisDomainService().repository.read_file(
            self.connection, self.scan["id"], self.file_id, [(10, 10)]
        )
        self.assertEqual(len(overlay), 1)
        self.assertEqual(overlay[0]["rejection_id"], rejection["id"])

        undone = rejections.undo(
            self.connection, project_id, self.scan["id"], line_id,
            rejection["id"], int(rejection["rejection_revision"]), 2,
        )
        self.connection.commit()
        active_link = self.connection.execute(
            "SELECT is_active, review_state, relation_revision "
            "FROM coverage_analysis_line_links WHERE id=?", (link["id"],)
        ).fetchone()
        self.assertEqual(tuple(active_link), (1, INHERITED_PENDING, 3))
        self.assertEqual(undone["terminal_reason"], "UNDONE")
        active_overlay = AnalysisDomainService().repository.read_file(
            self.connection, self.scan["id"], self.file_id, [(10, 10)]
        )
        self.assertEqual(active_overlay[0]["review_state"], INHERITED_PENDING)
        self.assertEqual(active_overlay[0]["relation_is_active"], 1)

    def test_manual_reanalysis_terminates_active_rejection_and_blocks_undo(self):
        project_id = self.connection.execute(
            "SELECT project_id FROM coverage_scans WHERE id=?", (self.scan["id"],)
        ).fetchone()[0]
        ProjectStateRepository().ensure(
            self.connection, project_id, current_scan_id=self.scan["id"]
        )
        self.connection.execute(
            "UPDATE coverage_project_state SET current_scan_id=? WHERE project_id=?",
            (self.scan["id"], project_id),
        )
        domain = AnalysisDomainService().repository
        record = domain.create_record(
            self.connection, {"status": "未确认", "coverage_method": "unit"},
            origin="INHERITED",
        )
        source_record = domain.create_record(
            self.connection, {"status": "可覆盖"}, origin="MANUAL"
        )
        source_line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE line_number=11"
        ).fetchone()[0]
        source_link = domain.create_link(
            self.connection, self.scan["id"], source_line_id, source_record["id"],
            review_state="MANUAL_CONFIRMED", relation_origin="MANUAL",
        )
        line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE line_number=10"
        ).fetchone()[0]
        link = domain.create_link(
            self.connection, self.scan["id"], line_id, record["id"],
            review_state=INHERITED_PENDING, relation_origin="INHERITANCE",
            source_scan_id=self.scan["id"], source_line_id=source_line_id,
            source_relation_id=source_link["id"],
        )
        self.connection.commit()
        rejection = InheritanceRejectionService().reject(
            self.connection, project_id, self.scan["id"], line_id, "alice",
            int(link["relation_revision"]),
        )
        self.connection.commit()
        AnalysisService().save(
            self.connection, "domain", self.scan["id"],
            [{"line_id": line_id, "status": "可覆盖", "coverage_method": "manual",
              "reviewer": "bob"}],
            enforce_current=True,
        )
        self.connection.commit()
        active_rejection = self.connection.execute(
            "SELECT is_active, terminal_reason FROM coverage_inheritance_rejections "
            "WHERE id=?", (rejection["id"],)
        ).fetchone()
        self.assertEqual(tuple(active_rejection), (0, "MANUAL_REANALYSIS"))
        with self.assertRaises(ValueError) as raised:
            InheritanceRejectionService().undo(
                self.connection, project_id, self.scan["id"], line_id,
                rejection["id"], 1, int(link["relation_revision"]) + 1,
            )
        self.assertEqual(str(raised.exception), "REJECTION_NOT_ACTIVE")

    def test_progress_pending_partition_is_conserved(self):
        project = self.projects.ensure_project(self.connection, "progress-domain")
        scan = self.projects.create_scan(
            self.connection, project["id"], "progress-scan", "import", "full",
            status="building",
        )
        file_row = self.projects.ensure_file(
            self.connection, scan["id"], "", "p" * 32, "src/progress.c", "progress.c"
        )
        self.lines.upsert_lines(self.connection, file_row["id"], [
            {"line_number": number, "line_text": "return {};".format(number),
             "coverage_state": "uncovered"}
            for number in (1, 2, 3, 4)
        ])
        self.projects.seal_scan(self.connection, scan["id"])
        from app.scan_import.publication import ScanPublicationService
        state = ProjectStateRepository().ensure(
            self.connection, project["id"], current_scan_id=None
        )
        ScanPublicationService(ProjectStateRepository()).publish_in_transaction(
            self.connection, project["id"], scan["id"],
            expected_current_scan_id=None,
        )
        service = AnalysisService()
        service.save(
            self.connection, "progress-domain", scan["id"],
            [{"file_path_hash": "p" * 32, "line_number": 2,
              "status": "未确认", "is_draft": True},
             {"file_path_hash": "p" * 32, "line_number": 4,
              "status": "可覆盖", "is_draft": False}],
        )
        domain = AnalysisDomainService().repository
        source_scan = self.projects.create_scan(
            self.connection, project["id"], "progress-source-scan", "import", "full",
            status="building",
        )
        source_file = self.projects.ensure_file(
            self.connection, source_scan["id"], "", "s" * 32,
            "src/progress-source.c", "progress-source.c"
        )
        self.lines.upsert_lines(self.connection, source_file["id"], [
            {"line_number": 3, "line_text": "return source;",
             "coverage_state": "uncovered"}
        ])
        self.projects.seal_scan(self.connection, source_scan["id"])
        inherited_record = domain.create_record(
            self.connection, {"status": "未确认"}, origin="INHERITED"
        )
        source_record = domain.create_record(
            self.connection, {"status": "可覆盖"}, origin="MANUAL"
        )
        source_line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE file_id=? AND line_number=3",
            (source_file["id"],),
        ).fetchone()[0]
        source_link = domain.create_link(
            self.connection, source_scan["id"], source_line_id, source_record["id"],
            review_state="MANUAL_CONFIRMED", relation_origin="MANUAL",
        )
        line_three = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE file_id=? AND line_number=3",
            (file_row["id"],),
        ).fetchone()[0]
        domain.create_link(
            self.connection, scan["id"], line_three, inherited_record["id"],
            review_state=INHERITED_PENDING, relation_origin="INHERITANCE",
            source_scan_id=source_scan["id"], source_line_id=source_line_id,
            source_relation_id=source_link["id"],
        )
        self.connection.commit()
        ProjectStateRepository().advance(self.connection, project["id"])
        summary = ProgressService().rebuild(
            self.connection, "progress-domain", scan["id"]
        )
        self.assertEqual(summary["pending_total"], 3)
        self.assertEqual(summary["ordinary_pending_total"], 1)
        self.assertEqual(summary["manual_draft_pending_total"], 1)
        self.assertEqual(summary["inherited_pending_total"], 1)
        self.assertEqual(summary["pending_conservation"]["status"], "PASSED")

    def test_domain_write_rejects_cross_scan_line_and_block_identity(self):
        project = self.projects.ensure_project(self.connection, "identity-domain")
        other_scan = self.projects.create_scan(
            self.connection, project["id"], "identity-scan", "import", "full",
            status="building",
        )
        other_file = self.projects.ensure_file(
            self.connection, other_scan["id"], "", "q" * 32, "src/q.c", "q.c"
        )
        self.lines.upsert_lines(self.connection, other_file["id"], [
            {"line_number": 1, "line_text": "return 1;", "coverage_state": "uncovered"}
        ])
        record = AnalysisDomainService().repository.create_record(
            self.connection, {"status": "未确认"}
        )
        with self.assertRaises(ValueError) as raised:
            AnalysisDomainService().repository.create_link(
                self.connection, self.scan["id"],
                self.connection.execute(
                    "SELECT id FROM coverage_lines WHERE file_id=?",
                    (other_file["id"],),
                ).fetchone()[0], record["id"],
            )
        self.assertEqual(str(raised.exception), "LINE_SCAN_IDENTITY_MISMATCH")
        with self.assertRaises(ValueError) as raised:
            AnalysisDomainService().repository.create_block(
                self.connection, self.scan["id"], other_file["id"], 1, 1,
            )
        self.assertEqual(
            str(raised.exception), "ANALYSIS_BLOCK_FILE_SCAN_IDENTITY_MISMATCH"
        )


if __name__ == "__main__":
    unittest.main()
