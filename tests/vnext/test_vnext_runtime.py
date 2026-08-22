import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import contextmanager
from unittest import mock
from decimal import Decimal
from datetime import datetime

from app.api.application import VNextApplication
from app.api.serialization import to_jsonable
from app.bootstrap import VNextRuntime, create_vnext_server
from app.code_detail.sidecar_store import SidecarStore
from app.code_detail.source_reader import SourceContext, SourceLineDTO, calc_sidecar_file_key
from app.code_detail.code_region import FunctionRange
from app.services.project_service import ProjectService
from scripts.upgrade.migration_runner import create_sqlite_schema
from tests.vnext.release_fixture import prepare_release_root


class VNextRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        create_sqlite_schema(self.connection)
        self.release_root = tempfile.TemporaryDirectory(prefix="vnext-release-")
        prepare_release_root(self.release_root.name)
        config = {
            "project_name": "fixture",
            "auth": {"mode": "disabled"},
            "runtime_state": {"root": tempfile.mkdtemp(prefix="vnext-state-")},
        }
        self.runtime = VNextRuntime(
            config, self.release_root.name, connection=self.connection
        )
        self.application = self.runtime.application()

    def tearDown(self):
        self.runtime.close()
        self.connection.close()
        self.release_root.cleanup()

    def test_scan_identity_separates_old_new_commit_pairs(self):
        base = [{
            "repository_name": "repo-a", "branch_name": "main",
            "commit_sha": "new", "old_commit_sha": "old-a",
            "new_commit_sha": "new",
        }]
        changed = [dict(base[0], old_commit_sha="old-b")]
        self.assertNotEqual(
            ProjectService.scan_key("fixture", "info", base, "full"),
            ProjectService.scan_key("fixture", "info", changed, "full"),
        )

    def test_runtime_metrics_expose_cache_and_resource_counters(self):
        status, payload = self.application.dispatch("GET", "/api/coverage/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(payload["runtime"], "vnext")
        self.assertIn("resources", payload["jobs"])
        self.assertIn("code_detail", payload)

    def test_scan_analysis_freshness_and_report_identity(self):
        status, body = self.application.dispatch(
            "POST", "/api/coverage/projects", body={"project_name": "fixture"}
        )
        self.assertEqual(status, 201)
        status, body = self.application.dispatch(
            "POST", "/api/coverage/scans",
            body={
                "project_name": "fixture",
                "info_sha256": "a" * 64,
                "report": {"report_id": "report_fixture"},
                "repositories": [{
                    "repository_name": "repo-a", "repository_path": "/candidate/repo-a",
                    "branch_name": "main", "old_commit_sha": "1" * 40,
                    "new_commit_sha": "2" * 40, "verified": True,
                }],
            }
        )
        self.assertEqual(status, 201)
        scan_id = body["scan"]["id"]
        self.runtime.project_service.ingest_files(
            self.connection, scan_id, [{
                "repository_name": "repo-a",
                "file_path": "src/fixture.c",
                "file_path_hash": "f" * 32,
                "lines": [
                    {"line_number": 10, "line_text": "fixture();",
                     "coverage_state": "uncovered", "block_start_line": 10,
                     "block_end_line": 10, "code_line_hash": "l10"},
                    {"line_number": 11, "line_text": "fixture2();",
                     "coverage_state": "uncovered", "block_start_line": 11,
                     "block_end_line": 11, "code_line_hash": "l11"},
                ],
            }]
        )
        status, pending = self.application.dispatch(
            "GET", "/api/coverage/incremental/unanalyzed",
            query={"project": "fixture"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(pending["files"][0]["pending_line_numbers"], [10, 11])

        line_rows = self.connection.execute(
            "SELECT id FROM coverage_lines ORDER BY line_number"
        ).fetchall()
        status, saved = self.application.dispatch(
            "POST", "/api/coverage/analysis",
            body={
                "project_name": "fixture", "scan_id": scan_id, "reviewer": "alice",
                "records": [{"line_id": line_rows[0][0], "status": "可覆盖",
                             "coverage_method": "unit"}],
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["saved"], 1)
        status, pending = self.application.dispatch(
            "GET", "/api/coverage/incremental/unanalyzed",
            query={"project": "fixture"},
        )
        self.assertEqual(pending["files"][0]["pending_line_numbers"], [11])
        status, pending_page = self.application.dispatch(
            "GET", "/api/coverage/progress/pending",
            query={"project": "fixture", "page": "1", "page_size": "1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(pending_page["total"], 1)
        self.assertEqual(pending_page["rows"][0]["line_number"], 11)
        self.runtime.progress_service.rebuild(self.connection, "fixture", scan_id)
        progress_trace = []
        def trace_progress(statement):
            progress_trace.append(statement)
        self.connection.set_trace_callback(trace_progress)
        summary = self.application.dispatch(
            "GET", "/api/coverage/progress", query={"project": "fixture"}
        )[1]
        self.connection.set_trace_callback(None)
        self.assertEqual(summary["source"], "coverage_file_state")
        self.assertEqual(summary["pending_total"], 1)
        self.assertFalse(
            any("SELECT * FROM coverage_file_state" in statement for statement in progress_trace)
        )
        self.assertFalse(
            any("FROM coverage_lines" in statement for statement in progress_trace)
        )
        report = self.connection.execute(
            "SELECT scan_id FROM coverage_reports WHERE report_id = ?",
            ("report_fixture",),
        ).fetchone()
        self.assertEqual(report[0], scan_id)

    def test_serializer_handles_cross_layer_values(self):
        value = to_jsonable({
            "amount": Decimal("1.50"),
            "when": datetime(2026, 8, 20, 1, 2, 3),
            "items": {"a", "b"},
        })
        self.assertEqual(value["amount"], 1.5)
        self.assertEqual(value["when"], "2026-08-20T01:02:03")
        self.assertEqual(sorted(value["items"]), ["a", "b"])

    def test_info_import_creates_immutable_scan_and_function_fallback(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".info", delete=False) as stream:
            stream.write(
                "TN:\n"
                "SF:src/imported.c\n"
                "DA:10,0\n"
                "DA:11,1\n"
                "FNL:0,10\n"
                "FNA:0,1,main\n"
                "end_of_record\n"
            )
            info_path = stream.name
        try:
            with self.runtime.connection_context() as connection:
                result = self.runtime.scan_import_service.import_info(
                    connection, "imported", info_path
                )
            self.assertEqual(result["files"], 1)
            self.assertEqual(result["line_count"], 2)
            self.assertEqual(result["function_range_fallback_files"], 1)
            scan_count = self.connection.execute(
                "SELECT COUNT(*) FROM coverage_scans WHERE project_id = "
                "(SELECT id FROM coverage_projects WHERE project_name = 'imported')"
            ).fetchone()[0]
            self.assertEqual(scan_count, 1)
        finally:
            os.remove(info_path)

    def test_api_info_import_enqueues_durable_coordinator_without_publishing_inline(self):
        self.connection.execute("""
            INSERT INTO coverage_repository_resources(
                resource_key, resolved_git_common_dir, resolved_worktree_root,
                next_fencing_token, observed_at
            ) VALUES ('api-resource', '/tmp/common', '/tmp/worktree', 0,
                      CURRENT_TIMESTAMP)
        """)
        self.connection.commit()
        resource_id = self.connection.execute(
            "SELECT id FROM coverage_repository_resources WHERE resource_key=?",
            ("api-resource",),
        ).fetchone()[0]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".info", delete=False) as stream:
            stream.write("TN:\nSF:src/api.c\nDA:1,0\nend_of_record\n")
            info_path = stream.name
        try:
            with mock.patch.object(
                self.runtime.job_service, "submit",
                return_value={"job_id": "queued-api-import", "state": "queued"},
            ) as submit:
                status, body = self.application.dispatch(
                    "POST", "/api/coverage/scans",
                    body={
                        "project_name": "api-import",
                        "info_path": info_path,
                        "repositories": [{
                            "repository_name": "repo",
                            "physical_resource_id": resource_id,
                        }],
                    },
                )
                self.assertEqual(status, 202)
                self.assertEqual(body["job"]["job_id"], "queued-api-import")
                self.assertNotIn("owner_token", body)
                self.assertNotIn("locks", body)
                self.assertEqual(
                    self.connection.execute(
                        "SELECT current_scan_id FROM coverage_project_state "
                        "WHERE project_id=(SELECT id FROM coverage_projects "
                        "WHERE project_name='api-import')"
                    ).fetchone()[0], None
                )
                submit.assert_called_once()
                submit.call_args[1]["callback"]()
            scan_id = body["scan"]["id"]
            self.assertEqual(
                self.connection.execute(
                    "SELECT current_scan_id FROM coverage_project_state "
                    "WHERE project_id=(SELECT id FROM coverage_projects "
                    "WHERE project_name='api-import')"
                ).fetchone()[0], scan_id
            )
        finally:
            os.remove(info_path)

    def test_real_stdlib_http_transport_uses_one_api_base(self):
        config = {
            "project_name": "fixture",
            "auth": {"mode": "disabled"},
            "runtime_state": {"root": tempfile.mkdtemp(prefix="vnext-http-")},
        }
        server = create_vnext_server(
            ("127.0.0.1", 0), config, repo_root=self.release_root.name,
            connection=self.connection,
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        try:
            url = "http://127.0.0.1:{}/api/coverage/health".format(
                server.server_address[1]
            )
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["runtime"], "vnext")
            self.assertEqual(payload["schema_version"], 1)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_code_detail_binds_exact_scan_report_and_sidecar_key(self):
        with tempfile.TemporaryDirectory(prefix="vnext-report-") as report_root:
            key = calc_sidecar_file_key("src/detail.c")
            context = SourceContext(
                "detail", "src/detail.c", [
                    SourceLineDTO(1, "return 0;", coverage_state="uncovered"),
                    SourceLineDTO(2, "return 1;", coverage_state="covered"),
                ], function_ranges=[FunctionRange(1, 2, "main")],
                report_id="report_detail",
            )
            SidecarStore([report_root], chunk_size=2).save_chunked_sidecar(
                report_root, "report_detail", key, context
            )
            self.runtime.report_registry.register(
                "report_detail", [report_root], sidecar_required=True
            )
            with self.runtime.connection_context() as connection:
                scan = self.runtime.project_service.create_scan(
                    connection, "detail", info_sha256="d" * 64,
                    report={"report_id": "report_detail", "report_root": report_root},
                )
                self.runtime.project_service.ingest_files(
                    connection, scan["id"], [{
                        "file_path": "src/detail.c", "file_path_hash": "" * 0,
                        "lines": [{"line_number": 1, "coverage_state": "uncovered"}],
                    }],
                )
                layout = self.runtime.code_detail.layout(
                    connection, scan["id"], "report_detail", "src/detail.c"
                )
                self.assertEqual(layout["pending_line_count"], 1)
                lines = self.runtime.code_detail.lines(
                    connection, scan["id"], "report_detail", "src/detail.c", 1, 1
                )
                self.assertEqual(lines["lines"][0]["line_no"], 1)
                with self.assertRaises(KeyError):
                    self.runtime.code_detail.layout(
                        connection, scan["id"], "report_other", "src/detail.c"
                    )

    def test_code_detail_batch_resolves_overlay_once(self):
        with tempfile.TemporaryDirectory(prefix="vnext-batch-report-") as report_root:
            key = calc_sidecar_file_key("src/batch.c")
            context = SourceContext(
                "batch", "src/batch.c", [
                    SourceLineDTO(1, "one();", coverage_state="uncovered"),
                    SourceLineDTO(2, "two();", coverage_state="uncovered"),
                    SourceLineDTO(3, "three();", coverage_state="covered"),
                    SourceLineDTO(4, "four();", coverage_state="covered"),
                ], report_id="report_batch",
            )
            SidecarStore([report_root], chunk_size=2).save_chunked_sidecar(
                report_root, "report_batch", key, context
            )
            self.runtime.report_registry.register(
                "report_batch", [report_root], sidecar_required=True
            )
            with self.runtime.connection_context() as connection:
                scan = self.runtime.project_service.create_scan(
                    connection, "batch", info_sha256="b" * 64,
                    report={"report_id": "report_batch", "report_root": report_root},
                )
                self.runtime.project_service.ingest_files(
                    connection, scan["id"], [{
                        "file_path": "src/batch.c", "file_path_hash": "" * 0,
                        "lines": [
                            {"line_number": 1, "coverage_state": "uncovered"},
                            {"line_number": 2, "coverage_state": "uncovered"},
                            {"line_number": 3, "coverage_state": "covered"},
                            {"line_number": 4, "coverage_state": "covered"},
                        ],
                    }],
                )
                trace = []
                def trace_statement(statement):
                    trace.append(statement)
                connection.set_trace_callback(trace_statement)
                batches = self.runtime.code_detail.lines_batch(
                    connection, scan["id"], "report_batch", "src/batch.c",
                    [(1, 1), (2, 2), (3, 4)],
                )
                # A second HTTP-equivalent request for the same immutable
                # data_version reuses the overlay instead of rereading the
                # same analysis rows.
                self.runtime.code_detail.lines_batch(
                    connection, scan["id"], "report_batch", "src/batch.c",
                    [(1, 1), (2, 2), (3, 4)],
                )
                connection.set_trace_callback(None)

            self.assertEqual([len(item["lines"]) for item in batches], [1, 1, 2])
            analysis_reads = [
                statement for statement in trace
                if "FROM coverage_analyses" in statement
            ]
            self.assertEqual(
                len(analysis_reads), 1,
                "batch code detail should load the file overlay once",
            )

            leases = []

            @contextmanager
            def tracked_read_context(read_only=False):
                lease = {"active": True}
                leases.append(lease)
                try:
                    yield self.connection
                finally:
                    lease["active"] = False

            sidecar = next(iter(self.runtime.code_detail._sidecar_stores.values()))
            original_metadata = sidecar.load_metadata
            original_ranges = sidecar.load_lines_ranges

            def assert_metadata_outside_db_lease(*args, **kwargs):
                self.assertFalse(
                    any(item["active"] for item in leases),
                    "Sidecar metadata must not be read while a DB lease is active",
                )
                return original_metadata(*args, **kwargs)

            def assert_ranges_outside_db_lease(*args, **kwargs):
                self.assertFalse(
                    any(item["active"] for item in leases),
                    "Sidecar chunks must not be decoded while a DB lease is active",
                )
                return original_ranges(*args, **kwargs)

            with mock.patch.object(
                self.runtime, "connection_context", tracked_read_context
            ), mock.patch.object(
                sidecar, "load_metadata", side_effect=assert_metadata_outside_db_lease
            ), mock.patch.object(
                sidecar, "load_lines_ranges", side_effect=assert_ranges_outside_db_lease
            ):
                status, response = self.application.dispatch(
                    "POST", "/api/coverage/code-lines/batch",
                    body={
                        "scan_id": scan["id"], "report_id": "report_batch",
                        "file_path": "src/batch.c",
                        "ranges": [{"start_line": 1, "end_line": 4}],
                    },
                )
            self.assertEqual(status, 200)
            self.assertEqual(len(response["batches"]), 1)
            self.assertEqual(len(leases), 2)
            self.assertTrue(all(not item["active"] for item in leases))

    def test_sidecar_shared_physical_chunk_is_decoded_once(self):
        with tempfile.TemporaryDirectory(prefix="vnext-sidecar-cache-") as root:
            store = SidecarStore([root], chunk_size=4)
            context = SourceContext(
                "cache", "src/cache.c", [
                    SourceLineDTO(i, "line{}".format(i), coverage_state="covered")
                    for i in range(1, 9)
                ], report_id="report_cache",
            )
            key = calc_sidecar_file_key("src/cache.c")
            store.save_chunked_sidecar(root, "report_cache", key, context)
            with mock.patch("app.code_detail.sidecar_store.json.load", wraps=json.load) as load:
                first = store.load_lines_ranges(
                    "report_cache", key, [(1, 2), (3, 4)]
                )
                second = store.load_lines_ranges(
                    "report_cache", key, [(1, 2), (3, 4)]
                )
                self.assertEqual(load.call_count, 2, "meta + one physical chunk")
            stats = store.cache_stats()
            self.assertEqual(stats["metadata_reads"], 1)
            self.assertGreaterEqual(stats["metadata_cache_hits"], 1)
            self.assertEqual(stats["chunk_reads"], 1)
            self.assertGreaterEqual(stats["chunk_cache_hits"], 1)
            self.assertEqual(stats["path_resolution_reads"], 1)
        self.assertEqual([[line["line_no"] for line in rows] for rows in first], [[1, 2], [3, 4]])
        self.assertEqual([[line["line_no"] for line in rows] for rows in second], [[1, 2], [3, 4]])

    def test_sidecar_concurrent_chunk_miss_is_single_flight(self):
        with tempfile.TemporaryDirectory(prefix="vnext-sidecar-single-flight-") as root:
            store = SidecarStore([root], chunk_size=4)
            context = SourceContext(
                "single-flight", "src/single-flight.c", [
                    SourceLineDTO(i, "line{}".format(i), coverage_state="covered")
                    for i in range(1, 9)
                ], report_id="report_single_flight",
            )
            key = calc_sidecar_file_key("src/single-flight.c")
            store.save_chunked_sidecar(root, "report_single_flight", key, context)
            store.load_metadata("report_single_flight", key)

            chunk_started = threading.Event()
            release_chunk = threading.Event()
            chunk_loads = []
            errors = []
            results = []
            original_load = json.load

            def slow_chunk_load(stream, *args, **kwargs):
                if os.path.basename(stream.name).startswith("lines-"):
                    chunk_loads.append(stream.name)
                    chunk_started.set()
                    if len(chunk_loads) == 1:
                        self.assertTrue(release_chunk.wait(5))
                return original_load(stream, *args, **kwargs)

            def worker():
                try:
                    results.append(store.load_lines_ranges(
                        "report_single_flight", key, [(1, 2), (3, 4)]
                    ))
                except BaseException as exc:  # pragma: no cover - assertion aid
                    errors.append(exc)

            with mock.patch("app.code_detail.sidecar_store.json.load",
                            side_effect=slow_chunk_load):
                first = threading.Thread(target=worker)
                second = threading.Thread(target=worker)
                first.start()
                self.assertTrue(chunk_started.wait(5))
                second.start()
                deadline = time.time() + 5
                while (store.cache_stats()["chunk_inflight_waits"] < 1 and
                       time.time() < deadline):
                    time.sleep(0.01)
                release_chunk.set()
                first.join(5)
                second.join(5)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 2)
            self.assertEqual(len(chunk_loads), 1)
            self.assertEqual(store.cache_stats()["chunk_reads"], 1)
            self.assertGreaterEqual(store.cache_stats()["chunk_inflight_waits"], 1)


if __name__ == "__main__":
    unittest.main()
