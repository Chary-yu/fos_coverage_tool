import json
import os
import sqlite3
import tempfile
import unittest
import zipfile

from app.bootstrap import VNextRuntime
from app.code_detail.sidecar_store import SidecarStore
from app.services.analysis_service import AnalysisService
from app.services.export_service import ExportService
from app.services.project_service import ProjectService
from app.db.repositories.analysis_domain_repository import INHERITED_PENDING
from scripts.upgrade.migration_runner import create_sqlite_schema
from tests.vnext.release_fixture import prepare_release_root


class VNextApiExportSecurityTest(unittest.TestCase):
    def setUp(self):
        self.state_root = tempfile.mkdtemp(prefix="vnext-api-state-")
        self.release_root = tempfile.TemporaryDirectory(prefix="vnext-api-release-")
        prepare_release_root(self.release_root.name)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_sqlite_schema(self.connection)

    def tearDown(self):
        runtime = getattr(self, "runtime", None)
        if runtime is not None:
            runtime.close()
        self.connection.close()
        self.release_root.cleanup()

    def _runtime(self, auth=None, server=None):
        config = {
            "project_name": "fixture",
            "auth": auth or {"mode": "disabled"},
            "runtime_state": {"root": self.state_root},
        }
        if server is not None:
            config["server"] = server
        self.runtime = VNextRuntime(
            config, self.release_root.name, connection=self.connection
        )
        return self.runtime

    def test_mutation_auth_origin_and_write_freeze_are_uniform(self):
        runtime = self._runtime({
            "mode": "reverse_proxy",
            "trusted_proxy_addresses": ["10.0.0.5"],
            "user_header": "X-Remote-User",
            "allowed_origins": ["https://candidate.example"],
        })
        app = runtime.application()
        status, _ = app.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "fixture"},
            remote_address="10.0.0.5",
        )
        self.assertEqual(status, 401)
        status, _ = app.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "fixture"},
            headers={"X-Remote-User": "alice"}, remote_address="10.0.0.6",
        )
        self.assertEqual(status, 401)
        status, _ = app.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "fixture"},
            headers={"X-Remote-User": "alice", "Origin": "https://evil.example"},
            remote_address="10.0.0.5",
        )
        self.assertEqual(status, 403)
        status, _ = app.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "fixture"},
            headers={"X-Remote-User": "alice", "Origin": "https://candidate.example"},
            remote_address="10.0.0.5",
        )
        self.assertEqual(status, 201)

        marker = os.path.join(self.state_root, "upgrade-writes-frozen.json")
        with open(marker, "w") as stream:
            stream.write("{}")
        status, payload = app.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "blocked"},
            headers={"X-Remote-User": "alice", "Origin": "https://candidate.example"},
            remote_address="10.0.0.5",
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "forbidden")
        status, payload = app.dispatch("GET", "/api/coverage/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["runtime"], "vnext")

    def test_authenticated_operator_reads_survive_write_freeze(self):
        runtime = self._runtime({
            "mode": "reverse_proxy",
            "trusted_proxy_addresses": ["10.0.0.5"],
            "user_header": "X-Remote-User",
        })
        app = runtime.application()
        marker = os.path.join(self.state_root, "upgrade-writes-frozen.json")
        with open(marker, "w") as stream:
            stream.write("{}")
        headers = {"X-Remote-User": "operator"}
        status, payload = app.dispatch(
            "GET", "/api/coverage/routes", headers=headers,
            remote_address="10.0.0.5",
        )
        self.assertEqual(status, 200)
        self.assertIn("routes", payload)
        status, _ = app.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "blocked"},
            headers=headers, remote_address="10.0.0.5",
        )
        self.assertEqual(status, 503)

    def test_non_loopback_bind_protects_data_reads(self):
        runtime = self._runtime({
            "mode": "reverse_proxy",
            "trusted_proxy_addresses": ["10.0.0.5"],
            "user_header": "X-Remote-User",
        }, server={"host": "0.0.0.0", "port": 9528})
        app = runtime.application()
        status, _ = app.dispatch("GET", "/api/coverage/projects", remote_address="10.0.0.5")
        self.assertEqual(status, 401)
        status, _ = app.dispatch(
            "GET", "/api/coverage/projects",
            headers={"X-Remote-User": "operator"}, remote_address="10.0.0.5",
        )
        self.assertEqual(status, 200)

    def test_disabled_auth_cannot_mutate_on_public_bind(self):
        runtime = self._runtime(
            {"mode": "disabled"}, server={"host": "0.0.0.0", "port": 9528}
        )
        status, payload = runtime.application().dispatch(
            "POST", "/api/coverage/projects",
            body={"project_name": "anonymous"}, remote_address="10.0.0.5",
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"], "forbidden")

    def test_malformed_identity_and_batch_limits_fail_closed(self):
        app = self._runtime().application()
        status, _ = app.dispatch("GET", "/api/coverage/code-layout", query={})
        self.assertEqual(status, 400)
        status, _ = app.dispatch(
            "GET", "/api/coverage/code-layout",
            query={"scan_id": 1, "report_id": "report", "file_path": "../escape.c"},
        )
        self.assertEqual(status, 400)
        status, _ = app.dispatch(
            "POST", "/api/coverage/code-lines/batch",
            body={"scan_id": 1, "report_id": "report", "file_path": "x.c",
                  "ranges": [{}] * 1001},
        )
        self.assertEqual(status, 400)
        status, _ = app.dispatch("GET", "/api/coverage/not-a-route")
        self.assertEqual(status, 404)

    def test_export_preserves_scan_identity_and_reviewer_semantics(self):
        project_repo = ProjectService()
        scan = project_repo.create_scan_and_ingest(
            self.connection, "export-project", [{
                "repository_name": "repo-a",
                "file_path": "src/a.c",
                "file_path_hash": "a" * 32,
                "lines": [{
                    "line_number": 10, "line_text": "return 0;",
                    "coverage_state": "uncovered", "suggested_reviewer": "git-alice",
                }],
            }],
            info_sha256="b" * 64,
            repositories=[{
                "repository_name": "repo-a", "repository_path": "/candidate/repo-a",
                "old_commit_sha": "1" * 40, "new_commit_sha": "2" * 40,
                "verified": True,
            }],
            report={"report_id": "export-report"},
        )
        line_id = self.connection.execute("SELECT id FROM coverage_lines").fetchone()[0]
        AnalysisService().save(
            self.connection, "export-project", scan["id"],
            [{"line_id": line_id, "status": "可覆盖", "reviewer": "db-carol",
              "coverage_method": "unit"}],
        )
        with tempfile.TemporaryDirectory(prefix="vnext-export-") as output_root:
            target = ExportService(
                project_repo.projects, output_root,
                release_identity={"build_id": "candidate-build"},
            ).export_scan(
                self.connection, "export-project", scan["id"], "export-report"
            )
            self.assertTrue(os.path.isfile(target))
            with zipfile.ZipFile(target) as archive:
                metadata = json.loads(archive.read("metadata.json").decode("utf-8"))
                rows = [json.loads(line) for line in archive.read(
                    "coverage_lines.jsonl").decode("utf-8").splitlines()]
            self.assertEqual(metadata["scan_id"], scan["id"])
            self.assertEqual(metadata["report_id"], "export-report")
            self.assertEqual(metadata["release"]["build_id"], "candidate-build")
            self.assertEqual(rows[0]["suggested_reviewer"], "git-alice")
            self.assertEqual(rows[0]["reviewer"], "db-carol")
            with self.assertRaises(ValueError):
                ExportService(project_repo.projects, output_root).export_scan(
                    self.connection, "export-project", scan["id"],
                    output_path=os.path.join(output_root, "..", "escape.zip"),
                )
                with self.assertRaises(KeyError):
                    ExportService(project_repo.projects, output_root).export_scan(
                        self.connection, "export-project", scan["id"], "wrong-report",
                    )

    def test_analysis_save_resolves_and_upserts_records_in_bulk(self):
        project_repo = ProjectService()
        scan = project_repo.create_scan_and_ingest(
            self.connection, "bulk-analysis", [{
                "repository_name": "repo-a",
                "file_path": "src/bulk.c",
                "file_path_hash": "b" * 32,
                "lines": [
                    {"line_number": 10, "coverage_state": "uncovered"},
                    {"line_number": 11, "coverage_state": "uncovered"},
                    {"line_number": 12, "coverage_state": "uncovered"},
                ],
            }], info_sha256="c" * 64,
        )
        records = [{
            "repository_name": "repo-a", "file_path_hash": "b" * 32,
            "line_number": line_number, "status": "可覆盖",
        } for line_number in (10, 11, 12)]
        trace = []
        def trace_statement(statement):
            trace.append(statement)
        self.connection.set_trace_callback(trace_statement)
        result = AnalysisService().save(
            self.connection, "bulk-analysis", scan["id"], records, reviewer="alice"
        )
        self.connection.set_trace_callback(None)

        self.assertEqual(result["saved"], 3)
        self.assertFalse(
            any("SELECT * FROM coverage_analyses WHERE line_id" in statement
                for statement in trace),
            "bulk save must not read each analysis row individually",
        )
        self.assertEqual(
            len([statement for statement in trace if "FROM coverage_analyses a" in statement]),
            1,
        )
        self.assertEqual(
            len([statement for statement in trace
                 if "INSERT INTO coverage_analysis_records" in statement]),
            1,
            "canonical manual records must use one bounded multi-row insert",
        )
        self.assertEqual(
            len([statement for statement in trace
                 if "INSERT INTO coverage_analysis_blocks" in statement]),
            1,
            "canonical analysis blocks must use one bounded multi-row insert",
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM coverage_analyses").fetchone()[0],
            3,
        )

    def test_analysis_save_batches_cas_and_shared_record_reference_reads(self):
        project_repo = ProjectService()
        scan = project_repo.create_scan_and_ingest(
            self.connection, "bulk-cas-analysis", [{
                "repository_name": "repo-a",
                "file_path": "src/cas.c",
                "file_path_hash": "d" * 32,
                "lines": [
                    {"line_number": line_number, "coverage_state": "uncovered"}
                    for line_number in (10, 11, 12)
                ],
            }], info_sha256="e" * 64,
        )
        service = AnalysisService()
        line_ids = [row[0] for row in self.connection.execute(
            "SELECT id FROM coverage_lines ORDER BY line_number"
        ).fetchall()]
        # Create three independent records so the follow-up request can
        # exercise several relation/content CAS checks in one save.
        for line_id in line_ids:
            service.save(
                self.connection, "bulk-cas-analysis", scan["id"],
                [{"line_id": line_id, "status": "可覆盖", "coverage_method": "unit"}],
            )
        identities = self.connection.execute("""
            SELECT line_id, analysis_record_id, relation_revision
            FROM coverage_analysis_line_links
            ORDER BY line_id
        """).fetchall()
        records = [{
            "line_id": int(line_id),
            "record_id": int(record_id),
            "expected_relation_revision": int(relation_revision),
            "expected_record_revision": 1,
            "status": "无法覆盖",
            "uncovered_reason": "batch-cas",
        } for line_id, record_id, relation_revision in identities]

        trace = []

        def record_trace(statement):
            trace.append(statement)

        self.connection.set_trace_callback(record_trace)
        result = service.save(
            self.connection, "bulk-cas-analysis", scan["id"], records,
            reviewer="alice",
        )
        self.connection.set_trace_callback(None)

        self.assertEqual(result["saved"], 3)
        relation_reads = [
            statement for statement in trace
            if "FROM coverage_analysis_line_links" in statement
        ]
        self.assertLessEqual(
            len(relation_reads), 4,
            "CAS/shared-record validation must use bounded batch relation reads",
        )
        self.assertFalse(
            any("WHERE analysis_record_id=" in statement for statement in relation_reads),
            "shared Record references must not be queried once per operation",
        )
        self.assertEqual(
            len([statement for statement in trace
                 if "INSERT INTO coverage_analysis_blocks" in statement]),
            1,
        )

    def test_http_analysis_uses_authenticated_reviewer_not_client_value(self):
        runtime = self._runtime({"mode": "disabled"})
        scan = runtime.project_service.create_scan_and_ingest(
            self.connection, "reviewer-auth", [{
                "repository_name": "repo-a", "file_path": "src/a.c",
                "file_path_hash": "r" * 32,
                "lines": [{"line_number": 1, "coverage_state": "uncovered"}],
            }], info_sha256="r" * 64,
        )
        line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE line_number=1"
        ).fetchone()[0]
        status, _ = runtime.application().dispatch(
            "POST", "/api/coverage/analysis", body={
                "project_name": "reviewer-auth", "scan_id": scan["id"],
                "reviewer": "client-spoof", "records": [{
                    "line_id": line_id, "status": "可覆盖",
                    "reviewer": "record-spoof", "coverage_method": "unit",
                }],
            },
        )
        self.assertEqual(status, 200)
        row = self.connection.execute(
            "SELECT reviewer FROM coverage_analyses WHERE line_id=?", (line_id,)
        ).fetchone()
        self.assertEqual(row[0], "anonymous")

    def test_sidecar_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="vnext-sidecar-") as root:
            outside = os.path.join(root, "outside")
            os.makedirs(outside)
            cache = os.path.join(root, ".source_cache")
            os.makedirs(cache)
            try:
                os.symlink(outside, os.path.join(cache, "report-a"))
            except (AttributeError, OSError):
                self.skipTest("symlink creation is unavailable")
            store = SidecarStore([root])
            self.assertIsNone(store.load_metadata("report-a", "file-key"))
            self.assertIsNone(store.load_lines_range("report-a", "file-key", 1, 2))

    def test_inheritance_reject_and_undo_api_is_current_and_revision_bound(self):
        runtime = self._runtime()
        scan = runtime.project_service.create_scan_and_ingest(
            self.connection, "inheritance-api", [{
                "repository_name": "repo-a",
                "file_path": "src/inherit.c",
                "file_path_hash": "i" * 32,
                "lines": [{
                    "line_number": 10, "line_text": "return 0;",
                    "coverage_state": "uncovered",
                }, {
                    "line_number": 11, "line_text": "return 1;",
                    "coverage_state": "uncovered",
                }],
            }],
            info_sha256="i" * 64,
        )
        project_id = self.connection.execute(
            "SELECT project_id FROM coverage_scans WHERE id=?", (scan["id"],)
        ).fetchone()[0]
        line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE line_number=10"
        ).fetchone()[0]
        record = runtime.analysis_domain_repository.create_record(
            self.connection, {"status": "可覆盖", "coverage_method": "unit"},
            origin="INHERITED",
        )
        source_record = runtime.analysis_domain_repository.create_record(
            self.connection, {"status": "可覆盖"}, origin="MANUAL"
        )
        source_line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE line_number=11"
        ).fetchone()[0]
        source_link = runtime.analysis_domain_repository.create_link(
            self.connection, scan["id"], source_line_id, source_record["id"],
            review_state="MANUAL_CONFIRMED", relation_origin="MANUAL",
        )
        link = runtime.analysis_domain_repository.create_link(
            self.connection, scan["id"], line_id, record["id"],
            review_state=INHERITED_PENDING, relation_origin="INHERITANCE",
            source_scan_id=scan["id"], source_line_id=source_line_id,
            source_relation_id=source_link["id"],
        )
        self.connection.commit()

        app = runtime.application()
        status, rejected = app.dispatch(
            "POST", "/api/coverage/scans/{}/inheritance/reject".format(scan["id"]),
            body={"line_id": line_id, "expected_relation_revision": link["relation_revision"]},
        )
        self.assertEqual(status, 200)
        rejection_id = rejected["rejection"]["id"]
        current_revision = self.connection.execute(
            "SELECT relation_revision FROM coverage_analysis_line_links WHERE id=?",
            (link["id"],),
        ).fetchone()[0]
        status, _ = app.dispatch(
            "POST", "/api/coverage/scans/{}/inheritance/rejections/{}/undo".format(
                scan["id"], rejection_id
            ),
            body={
                "line_id": line_id,
                "rejection_id": rejection_id,
                "expected_rejection_revision": 1,
                "expected_relation_revision": current_revision,
            },
        )
        self.assertEqual(status, 200)
        restored = self.connection.execute(
            "SELECT is_active, review_state, relation_revision "
            "FROM coverage_analysis_line_links WHERE id=?", (link["id"],)
        ).fetchone()
        self.assertEqual(tuple(restored), (1, INHERITED_PENDING, current_revision + 1))

        status, payload = app.dispatch(
            "POST", "/api/coverage/scans/{}/inheritance/reject".format(scan["id"]),
            body={"line_id": line_id, "expected_relation_revision": link["relation_revision"]},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "STALE_RELATION_REVISION")

    def test_inheritance_cursor_is_bound_to_scan_version_and_filter(self):
        runtime = self._runtime()
        scan = runtime.project_service.create_scan_and_ingest(
            self.connection, "cursor-project", [{
                "repository_name": "repo-a", "file_path": "src/cursor.c",
                "file_path_hash": "c" * 32,
                "lines": [
                    {"line_number": 10, "coverage_state": "uncovered"},
                    {"line_number": 11, "coverage_state": "uncovered"},
                ],
            }], info_sha256="cursor-info",
        )
        line_ids = [row[0] for row in self.connection.execute(
            "SELECT id FROM coverage_lines ORDER BY line_number"
        ).fetchall()]
        for index, line_id in enumerate(line_ids):
            self.connection.execute("""
                INSERT INTO coverage_inheritance_decisions(
                    decision_run_id, candidate_scan_id, candidate_line_id,
                    decision, reason_code, algorithm_version, evaluated_at
                ) VALUES (?, ?, ?, 'NO_INHERIT', ?, 'test', CURRENT_TIMESTAMP)
            """, ("cursor-run-{}".format(index), scan["id"], line_id,
                  "REASON-{}".format(index)))
        self.connection.commit()
        app = runtime.application()
        status, first = app.dispatch(
            "GET", "/api/coverage/scans/{}/inheritance/decisions".format(scan["id"]),
            query={"limit": 1},
        )
        self.assertEqual(status, 200)
        self.assertTrue(first["has_more"])
        cursor = first["next_cursor"]
        status, second = app.dispatch(
            "GET", "/api/coverage/scans/{}/inheritance/decisions".format(scan["id"]),
            query={"limit": 1, "cursor": cursor},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(second["items"]), 1)
        status, payload = app.dispatch(
            "GET", "/api/coverage/scans/{}/inheritance/decisions".format(scan["id"]),
            query={"limit": 1, "cursor": cursor, "reason_code": "REASON-1"},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "PAGINATION_CURSOR_STALE")
        project_id = self.connection.execute(
            "SELECT project_id FROM coverage_scans WHERE id=?", (scan["id"],)
        ).fetchone()[0]
        runtime.states.advance(self.connection, project_id)
        self.connection.commit()
        status, payload = app.dispatch(
            "GET", "/api/coverage/scans/{}/inheritance/decisions".format(scan["id"]),
            query={"limit": 1, "cursor": cursor},
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "PAGINATION_CURSOR_STALE")

    def test_inheritance_pending_excludes_confirmed_and_rejected_relations(self):
        runtime = self._runtime()
        scan = runtime.project_service.create_scan_and_ingest(
            self.connection, "pending-filter-project", [{
                "repository_name": "repo-a", "file_path": "src/pending.c",
                "file_path_hash": "p" * 32,
                "lines": [
                    {"line_number": 10, "coverage_state": "uncovered"},
                    {"line_number": 11, "coverage_state": "uncovered"},
                ],
            }], info_sha256="pending-filter-info",
        )
        line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE line_number=10"
        ).fetchone()[0]
        self.connection.execute("""
            INSERT INTO coverage_inheritance_decisions(
                decision_run_id, candidate_scan_id, candidate_line_id,
                decision, reason_code, algorithm_version, evaluated_at
            ) VALUES ('pending-filter-run', ?, ?, 'INHERITED', 'INHERITED', 'test', CURRENT_TIMESTAMP)
        """, (scan["id"], line_id))
        domain = runtime.analysis_domain_repository
        source_record = domain.create_record(
            self.connection, {"status": "可覆盖"}, origin="MANUAL"
        )
        source_line_id = self.connection.execute(
            "SELECT id FROM coverage_lines WHERE line_number=11"
        ).fetchone()[0]
        source_link = domain.create_link(
            self.connection, scan["id"], source_line_id, source_record["id"],
            review_state="MANUAL_CONFIRMED", relation_origin="MANUAL",
        )
        record = domain.create_record(
            self.connection, {"status": "未确认"}, origin="INHERITED"
        )
        link = domain.create_link(
            self.connection, scan["id"], line_id, record["id"],
            review_state=INHERITED_PENDING, relation_origin="INHERITANCE",
            source_scan_id=scan["id"], source_line_id=source_line_id,
            source_relation_id=source_link["id"],
        )
        self.connection.commit()
        app = runtime.application()
        status, payload = app.dispatch(
            "GET", "/api/coverage/scans/{}/inheritance/pending".format(scan["id"]),
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["items"]), 1)

        self.connection.execute(
            "UPDATE coverage_analysis_line_links SET review_state='MANUAL_CONFIRMED' "
            "WHERE id=?", (link["id"],)
        )
        self.connection.commit()
        status, payload = app.dispatch(
            "GET", "/api/coverage/scans/{}/inheritance/pending".format(scan["id"]),
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])


if __name__ == "__main__":
    unittest.main()
